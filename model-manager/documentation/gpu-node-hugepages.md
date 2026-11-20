# Huge pages on GPU serving nodes — THP (madvise) enablement

**This is a node-level tuning, not a chart value and not a Model Manager setting.** It is documented here because the `deepseek-v41-flash-1017` catalog entries in `seed_catalog.json` encode a serving configuration whose host-memory tier (the Engram host table) only reaches its intended behaviour when the node's transparent-hugepage knobs are set as below. If you deploy those entries onto an untuned node, the model still serves — it just silently runs its host table on 4 KiB pages, and every performance number you take there is not comparable to ours.

Everything below was applied on **`pcai-se-scs04.hst.lab`** (the internal **G2** cluster, 4×H200) on **2026-10-02**, re-verified **2026-10-17**. It is mode-only, needs no memory reservation, and is reversible by deleting one file. Re-read from the host on **2026-11-19** — still in force, no drift (§2).

## The short version

```bash
# on the node, as a privileged one-shot (see §5 for the audited Job form)
cat /sys/kernel/mm/transparent_hugepage/enabled        # -> always madvise [madvise] never
cat /sys/kernel/mm/transparent_hugepage/shmem_enabled  # -> ... [advise] ...   <-- the one that mattered
echo madvise > /sys/kernel/mm/transparent_hugepage/enabled
echo advise  > /sys/kernel/mm/transparent_hugepage/shmem_enabled

# persist across reboot (host disk, not the container's overlay)
cat > /etc/tmpfiles.d/enforce-thp.conf <<'EOF'
w /sys/kernel/mm/transparent_hugepage/enabled - - - - madvise
w /sys/kernel/mm/transparent_hugepage/shmem_enabled - - - - advise
EOF

# then RESTART the serving pod — the table is allocated at boot
```

Success is not "the echo returned 0". Success is the serving pod's own boot log:

```
engram host table layer 1: layout=shared, 96681 MiB resident, 100% huge pages
```

## 1. What we changed — and what we deliberately did not

| Knob | Before | After | Why |
|---|---|---|---|
| `/sys/kernel/mm/transparent_hugepage/enabled` | `[madvise]` | `[madvise]` (unchanged) | Already correct on this node; the line is kept in the runbook so a fresh node converges with one procedure instead of two |
| `/sys/kernel/mm/transparent_hugepage/shmem_enabled` | `[never]` | `[advise]` | **The actual blocker.** Governs THP for shmem/tmpfs/memfd mappings, which is where the shared-layout Engram host table lives. `never` makes the process's `madvise(MADV_HUGEPAGE)` a no-op — silently, no error, no warning |
| `/etc/tmpfiles.d/enforce-thp.conf` | absent | two `w` lines | systemd-tmpfiles re-applies mode-only writes at every boot, so the setting survives reboot without a daemon, a unit, or a sysctl |

**Deliberately not done** — this is why the change is safe and why it needs no capacity planning:

- **No hugetlbfs, no `vm.nr_hugepages`, no pre-reserved pool.** Nothing is taken from the node up front; pages become 2 MiB only where a process opts in with `madvise(MADV_HUGEPAGE)`. If nothing opts in, the node behaves exactly as before.
- **No kernel command line, no reboot-to-apply, no GRUB edit.**
- **No `defrag` change.** Left at its default; `madvise` mode already confines the work to processes that asked for it.
- **No application flag.** SGLang already issues `madvise(MADV_HUGEPAGE)` for the Engram host table. The kernel was refusing it; we stopped refusing it. There is nothing to set in the deployment spec.

## 2. What the tuned node looks like today (verified 2026-11-19)

Read from the hosts, not from memory, via a short-lived privileged probe pod on each node.

`pcai-se-scs04.hst.lab` — kernel `6.8.0-60-generic`, systemd 255:

| Check | Value |
|---|---|
| `transparent_hugepage/enabled` | `always [madvise] never` |
| `transparent_hugepage/shmem_enabled` | `always within_size [advise] never deny force` |
| `transparent_hugepage/defrag` | `always defer defer+madvise [madvise] never` — the kernel default in madvise mode; **we do not write it** |
| `hpage_pmd_size` | `2097152` (2 MiB) |
| `/etc/tmpfiles.d/enforce-thp.conf` | present, 129 bytes, `root:root`, mtime **2026-10-02 14:00** — exactly the two `w` lines above |
| registered with systemd-tmpfiles | yes — `systemd-tmpfiles --cat-config` lists it inside the host's own set (`00rsyslog.conf`, `lvm2.conf`, `snapd.conf`, …), not a container's |
| last write to either sysfs knob | **2026-10-02 14:01:58 UTC** — nothing has re-asserted or reverted them since |
| `/proc/meminfo` | `ShmemHugePages: 198006784 kB`, `ShmemPmdMapped: 198002688 kB` (**≈189 GiB** of shmem on 2 MiB pages), `AnonHugePages: 321536 kB`, `HugePages_Total: 0` |
| host root filesystem | `/dev/mapper/VolGroup-lv_root / ext4 rw` — a real disk, so `/etc` is genuinely persistent |

The **189 GiB of `ShmemHugePages`** is the load-bearing reading: it can only exist if the kernel is honouring the madvise advice on shmem mappings, which is exactly what `shmem_enabled=never` was refusing. `HugePages_Total: 0` confirms nothing was reserved.

**The other GPU node, `pcai-se-scs05.hst.lab`, is untuned:** its `/etc/tmpfiles.d` is empty (no `enforce-thp.conf`, and `systemd-tmpfiles --cat-config | grep thp` returns nothing), and it hosts no DeepSeek pod. That is the cautionary row: the tuning is per node, and only one node in the pool has it. Anything schedulable onto `scs05` runs the table on 4 KiB pages and reports nothing.

One lesson from taking these readings: the serving pod's **own boot-log lines had already rotated away** (its boot was nine days earlier; the kubelet keeps ~10 days / 10 MiB per container). Capture the `huge pages` line at restart time — the node state and `ShmemHugePages` are what survive.

### Persistence: what protects this, and what does not

| Protects it | Does not |
|---|---|
| `systemd-tmpfiles-setup.service` applies the `w` lines at sysinit, so a **reboot** comes back tuned | It applies **once**. Nothing re-asserts THP later — a `tuned` profile or a later service that rewrote the knob would win, silently (none was found on either node, and neither GRUB cmdline carries THP arguments) |
| `/etc` is on a real ext4 volume, not an overlay | The file is **hand-placed on one node's `/etc`** — not in git, not Ansible, not a DaemonSet. A reimage, an `/etc` restore, or a PCAI reprovision drops it, and **nothing alerts** |
| `enabled` + `shmem_enabled` are both written | `defrag` is **not** written. It reads `[madvise]` today only because that is the kernel default in madvise mode; on a node whose default differs, add a third `w` line |
| — | `scs05` has none of it |

## 3. Why it matters: the Engram host table is a TLB problem

DeepSeek-V4.1-Flash carries a 196 B-parameter Engram — a conditional-memory lookup table, not a matmul. Serving it with `SGLANG_ENABLE_DSV41_ENGRAM_HOST_TABLE=1` keeps that table in **pinned host memory** — measured at **≈189 GiB** of shmem on the tuned node (§2), allocated in per-layer chunks — and every token performs hashed n-gram lookups scattered across it. That access pattern is the worst case for a 4 KiB-page TLB: the working set is enormous and the accesses are random, so each lookup can cost a page walk.

2 MiB pages cut the number of TLB entries covering the same table by ~512×. That is the whole mechanism — there is no exotic tuning here, just letting the process get the page size it asked for.

The failure mode is quiet, which is why this doc exists:

- With `shmem_enabled=never`, the kernel ignores the advice and backs the mapping with 4 KiB pages. Nothing logs an error. The model boots, serves, and is slower.
- The signals are the boot log line quoted above (or its negative form, `No huge pages: expect ~10x slower lookups`, when the tuning is missing) and `ShmemPmdMapped` in the process's `smaps` (§5.4).

## 4. Prerequisites

| Prerequisite | Why | Fails how without it |
|---|---|---|
| The node name, and a kubeconfig that can run privileged pods on it | The write happens in the host's sysfs, from a privileged container | `kubectl debug node/...` fails at admission, or the Job stays Pending |
| A path to the cluster API from where you are | On G2 the API is only reachable through the SOCKS proxy: `env https_proxy=socks5://127.0.0.1:1080 kubectl --kubeconfig ~/SSH/G2/g2_kubeconfig.yaml …` | Every call times out; it looks like a dead cluster |
| The node is the one your serving pod actually lands on | This is per-node state. A pod rescheduled to an untuned node loses the benefit silently | Perf numbers quietly regress; nothing is "broken" |
| A maintenance window for the serving pod | The table is allocated at boot, so the pod must be restarted | The running pod keeps its 4 KiB mappings; the knobs read "correct" while the table is still slow |

**Pin the workload to the tuned node(s), or tune every candidate node.** If the ISVC can be scheduled onto any node in the pool, the tuning is only as good as the worst node it can land on.

## 5. Procedure

### 5.1 The audited, one-shot form (preferred)

A privileged `Job` pinned with `nodeName`, writing the host's sysfs through a `hostPath` mount and the persistence file through a second `hostPath` mount of `/etc`. This is the deterministic form: it is a manifest, it is dry-run plan-validated before it is applied, it runs once (`backoffLimit: 0`), and it leaves nothing running.

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: thp-enable-scs04
  namespace: project-user-andrew-bydlon
spec:
  backoffLimit: 0
  template:
    metadata:
      labels:
        k8s-mcp.io/exec: "true"   # opts this pod into the fleet's k8s-mcp exec gate
    spec:
      nodeName: pcai-se-scs04.hst.lab
      restartPolicy: Never
      containers:
      - name: thp-writer
        image: busybox:1.36
        securityContext: { privileged: true }
        command: ["sh", "-c"]
        args:
        - |
          set -e
          SYS=/host-sys/kernel/mm/transparent_hugepage
          echo "== BEFORE =="; cat $SYS/enabled; cat $SYS/shmem_enabled
          echo madvise > $SYS/enabled
          echo advise  > $SYS/shmem_enabled
          echo "== AFTER ==";  cat $SYS/enabled; cat $SYS/shmem_enabled
          printf 'w /sys/kernel/mm/transparent_hugepage/enabled - - - - madvise\nw /sys/kernel/mm/transparent_hugepage/shmem_enabled - - - - advise\n' > /host-etc/tmpfiles.d/enforce-thp.conf
        volumeMounts:
        - { name: host-sys, mountPath: /host-sys }
        - { name: host-etc, mountPath: /host-etc }
      volumes:
      - { name: host-sys, hostPath: { path: /sys, type: Directory } }
      - { name: host-etc, hostPath: { path: /etc, type: Directory } }
```

Dry-run plan it first — the fleet's apply gate returns a plan hash before anything is applied (our original was `sha256:56c28fd4b73a2199…`, validated 2026-10-02), which is what makes this the auditable form — then apply, read the Job log for the BEFORE/AFTER pair, and delete the Job. The `k8s-mcp.io/exec` label is the per-pod opt-in the fleet's exec gate requires; without it you cannot shell into the Job's pod to re-read the knobs by hand.

If the AFTER lines in the Job log come back unchanged, the host mount is the problem — use §5.2's interactive form instead, which writes the debug container's own `/sys`. Read the bracket back either way (§5.3); that check is what makes the two forms equivalent in outcome.

### 5.2 The interactive form (quick path)

```bash
kubectl debug node/pcai-se-scs04.hst.lab -it --image=busybox:1.36 --profile=sysadmin
# inside: write the CONTAINER'S OWN /sys, and the host /etc through /host
echo madvise > /sys/kernel/mm/transparent_hugepage/enabled
echo advise  > /sys/kernel/mm/transparent_hugepage/shmem_enabled
cat /host/etc/tmpfiles.d/enforce-thp.conf 2>/dev/null || \
  printf 'w /sys/kernel/mm/transparent_hugepage/enabled - - - - madvise\nw /sys/kernel/mm/transparent_hugepage/shmem_enabled - - - - advise\n' \
  > /host/etc/tmpfiles.d/enforce-thp.conf
```

**The one gotcha that cost us time:** our first attempts wrote through the host mount (`/host/sys/...`) and did not take. The write that stuck was against the debug container's own `/sys`. Both paths reach the same kernel-global sysfs, so the difference is a mount/permission artifact rather than semantics — but do not assume: **read the knob back after writing it** (§5.3). If `/host-sys` comes back read-only, use the container's own `/sys`.

### 5.3 Verify the write, immediately

```bash
cat /sys/kernel/mm/transparent_hugepage/enabled        # always madvise [madvise] never
cat /sys/kernel/mm/transparent_hugepage/shmem_enabled  # always within_size advise [advise] never deny force
systemd-tmpfiles --cat-config | grep -i -B1 -A1 thp     # the two w-lines, as registered
```

The **bracketed** value is the active one. If the bracket is not on `madvise` / `advise`, the write did not land — do not proceed to the restart.

### 5.4 Restart the serving pod — this is required, not optional

The Engram host table allocates at boot, so a running process never picks up the change. Scale the ISVC to 0, confirm the GPUs drain (**0–2 GiB per GPU** — do not restart on top of a dirty island), then scale back up. Teardown of the pinned Engram takes **4–14 minutes**; KServe brings the pod back about a minute after it dies.

Then verify in two places:

```bash
# 1. the boot log — the real success marker
kubectl -n <ns> logs <sglang-pod> | grep -i "huge pages"
#    engram host table layer 1: layout=shared, 96681 MiB resident, 100% huge pages

# 2. inside the serving container — the direct proof the shmem path is engaging
PID=$(pgrep -f sglang | head -1)
grep ShmemPmdMapped /proc/$PID/smaps | awk '{s+=$2} END {print s/1024 " MiB shmem THP"}'
grep AnonHugePages   /proc/$PID/smaps | awk '{s+=$2} END {print s/1024 " MiB anon THP"}'
```

A boot log that says **`No huge pages: expect ~10x slower lookups`** is the engine telling you the apply did not take — that is the failure marker to grep for, and it is worth the ten seconds.

**Non-zero `ShmemPmdMapped` is the confirmation.** A `100% huge pages` line on every Engram layer is the boot-time confirmation. A percentage below 100 on some layers is the signal that the setting did not fully take (or that the node was re-imaged) — treat it as a failed apply, not as noise.

### 5.5 Rollback

```bash
rm /etc/tmpfiles.d/enforce-thp.conf
echo never > /sys/kernel/mm/transparent_hugepage/shmem_enabled
echo madvise > /sys/kernel/mm/transparent_hugepage/enabled   # leave enabled alone unless you have a reason
# restart the serving pod to drop back to 4 KiB-backed tables
```

There is no state to unwind beyond that: no pool was reserved, so nothing is returned to the node and nothing leaks.

## 6. Traps, in the order they bite

| Trap | Symptom | Fix |
|---|---|---|
| `shmem_enabled` left at `never` | Everything looks fine; the Engram line reports a low huge-page percentage, or none at all | Set it to `advise`. `enabled=madvise` alone is **not** sufficient — this was the whole bug |
| Applied, but the pod was never restarted | Knobs read correctly, throughput unchanged | Restart (scale 0 → drain → scale up). The table is allocated at boot |
| Applied on the wrong node | Works until the pod reschedules, then silently stops working | Pin the workload (`nodeSelector`/`nodeName`) or tune every node it can land on |
| Re-imaged / replaced node | Regression with no change on our side: the tmpfiles file is on `/etc` of the *old* disk | Re-run §5.1 on the new node; treat node replacement as a re-apply event |
| `echo` reported success but the knob is unchanged | `/host/sys` mount was read-only, or a `tuned` profile re-asserted THP | Read the bracket back (§5.3); check `tuned-adm active` |
| Restart on a dirty GPU island | Boot-time OOM, or a poisoned pool | Drain to 0–2 GiB per GPU first; a negative `mem usage=` line in the log voids the boot |
| No privileged pods allowed (customer-managed node) | You cannot do any of the above | Use the deployment-side alternative below |
| A `tuned` profile (or any later boot-time service) re-writes THP | Reverts without anyone touching our file; `--cat-config` still looks right | Check `tuned-adm active`; re-assert after that service, or disable its THP rule |
| A node whose kernel default for `defrag` is not `madvise` | `defrag` reads `always`, so reclaim works node-wide instead of per-mapping | Add a third `w` line for `defrag` — this is the one knob we deliberately left alone |

### The deployment-side alternative, when the node is not yours

`SGLANG_DSV41_ENGRAM_HOST_TABLE_LAYOUT=per_rank` sidesteps shmem THP entirely: the tables are allocated per rank as anonymous memory and collapsed with `MADV_COLLAPSE` (governed by `enabled`, not `shmem_enabled`), so no shared PID namespace and no shmem tuning is needed. It costs **~22.9 GiB of HBM per rank** and about **0.2 % ITL** in extra all-reduce. Prefer it when you cannot control the node; prefer the shared layout plus this tuning when you can.

## 7. Does this explain the DeepSeek-V4.1-Flash speedup? No — the timeline rules it out

The question gets asked because a benchmark dashboard shows `deepseek-v4.1-flash` (the reference, green) against `deepseek-v4.1-flash-1017` (blue) with a large gap. **That pair cannot be evidence of a huge-page effect, because the tuning was already on for both halves of it.** This is checkable, not a matter of opinion:

| | `deepseek-v4.1-flash` (ref) | `deepseek-v4.1-flash-1017` |
|---|---|---|
| measured | run started **2026-10-04 19:33 UTC** | **2026-10-17 → 2026-10-18 14:42 UTC** |
| node | scs04 | scs04 |
| THP | on (enabled 2026-10-02) | on — boot log: **14/14 Engram layers 100 % huge-paged** |
| engine | v0.5.21-cu130 (digest `b1259f3ea`) | `lmsysorg/sglang:latest` = v0.5.22 that day |
| mem-fraction | **0.70** | **0.80** |
| checkpoint | DeepSeek-V4.1-Flash (476 GB) | DeepSeek-V4.1-Flash-**1017** (2026-10-17 refresh) |

Both runs postdate the 2026-10-02 enablement, both landed on the same node, and the -1017 side's own boot log reports the table fully huge-paged. Huge pages are a **constant** in that comparison: the chart is evidence the tuning was working, not evidence that it helped.

What actually differs, ranked by how well it fits the shape of the curves:

1. **mem-fraction 0.70 → 0.80** — the capacity knob. The largest movement in the chart is TTFT at 64 concurrent users and throughput everywhere else, which is a queueing/pool-capacity signature, not a page-size signature.
2. **The checkpoint refresh** (the -1017 weights) — a per-token-speed variable.
3. **The engine minor** (v0.5.21 → v0.5.22/23) — real, but unquantified for this workload. The only number we hold is upstream's "v0.5.21 met both (#40352 = 22 % faster TTFT)", which is a prefill claim, not a decode claim.

**And the honest state of our record: no note anywhere attributes this bump to huge pages or to the SGLang update.** The only quantified huge-page claim we have is the engine's own boot warning when the tuning is *missing* — `No huge pages: expect ~10x slower lookups` — which is about the Engram lookup path, not decode throughput.

### The A/B that would actually measure it

Pin the image **by digest** for every arm; a floating `:latest` is precisely what made the original question unanswerable. Same node, same harness level set (users 1/4/32/64 × ctx 0/32768 × coding+creative, `/flush_cache` between arms), one variable at a time:

1. **THP A/B — the only one nobody has measured.** Same digest, same checkpoint, same flags, same mem-fraction; arm A `shmem_enabled=advise`, arm B `never`. Each arm needs scale-to-0 → GPU drain → scale-up, and each is verified by its own boot line (`100 % huge pages` vs the `No huge pages` warning). This tells you whether the tuning is worth keeping — it does **not** explain the 2026-10 chart, which held THP constant.
2. **Engine A/B.** Same node, THP held at `advise`, same checkpoint and flags: `v0.5.21-cu130` vs `v0.5.23-cu130`, by digest.
3. **Checkpoint A/B.** Same digest, flags and THP: V4.1-Flash vs V4.1-Flash-1017.

If you are re-running any of these to reproduce the dashboard gap, **equalize mem-fraction first** (0.775 is the recorded operating point) — otherwise you have rebuilt the original confound and will learn nothing.

## 8. Replication checklist

1. Confirm the node hosts (or will host) a DeepSeek-V4.1-Flash configuration with the **Engram host table** enabled — the tuning buys nothing for a deployment without one.
2. Record the before state: both knobs, plus one boot-log Engram line if the pod is already running.
3. Apply §5.1 (or §5.2), read the knobs back.
4. Confirm the tmpfiles file is registered (`systemd-tmpfiles --cat-config`).
5. Scale the ISVC to 0; wait for the GPUs to drain to 0–2 GiB.
6. Scale up; grep the boot log for `huge pages` — every layer at 100 %. Copy that line into your performance record: it rotates out of the kubelet's log within ~10 days, and then the only durable evidence is `ShmemHugePages` on the node.
7. Confirm `ShmemPmdMapped` is non-zero inside the serving container (or `ShmemHugePages` is ~189 GiB in the node's `/proc/meminfo` — that reading needs no exec).
8. Pin the workload to the tuned node(s), or repeat on every candidate node.
9. Note the date and the node in whatever performance record you keep — this is the variable most likely to be silently different between two "identical" runs.

## 9. Evidence trail

| Artifact | Where | Date |
|---|---|---|
| One-shot Job manifest (plan-validated) | `ModelDeploymentApproaches/scratch/thp-enable-scs04.job.yaml` | 2026-10-02 |
| Interactive one-liner used for the flip | `ModelDeploymentApproaches/apply_huge_pages.sh` | 2026-10-02 |
| Two-page write-up generated for a colleague | `ModelDeploymentApproaches/deepseek-g2-thp-madvise-enablement.pdf` | 2026-10-20 |
| Re-verification on the -1017 boot (14/14 layers, 100 %) | session record | 2026-10-17 |
| The measurement the catalog entry quotes | `seed_catalog.json`, `deepseek-v41-flash-1017` `notes` ("96681 MiB resident, 100% huge pages") | 2026-10-30 |
| THP guidance in the serving stack's own docs (layout trade-off, the `No huge pages` warning) | `ModelDeploymentApproaches/sproket/deepseek-v4.1/DSV41-SGLANG/docs/memory-ledger.md` | 2026-10 |
| Both sides of the benchmark pair measured with THP already on | `ModelBenchmarker/results/deepseek_v41_flash/H200x4_engram_offload.md` | 2026-10-04 / 2026-10-18 |
| Live node state re-read (both nodes, sysfs + `/proc/meminfo`) | this document, §2 | 2026-11-19 |

**A note on the PDF:** it was generated from session memory rather than from the cluster, and the first render came out mangled (byte-patched xref offsets). This markdown file is the source of record; regenerate any PDF from it rather than hand-editing one.
