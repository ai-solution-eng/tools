# Portability — these skills must work for anyone on the team

**Rule: no operator-specific values in this directory.** Not a home directory, not a dataset
name, not a namespace with someone's username in it, not a conda env name, not a kubeconfig path,
not a personal ops directory. If a value would be wrong on a teammate's machine, it does not
belong in a shared skill.

That is the whole rule. The rest of this file is how to follow it without losing the knowledge.

## What to write instead

| Instead of | Write |
|---|---|
| `/home/<someone>/Code/.../MultimodalRAG` | `<your MultimodalRAG checkout>` |
| `andrew-memory` | "discover it" — plus *how* to discover it |
| `project-user-<someone>` | `project-user-<you>` |
| `~/SSH/G2/g2_kubeconfig.yaml` | `<your G2 kubeconfig>` |
| conda env `ML14` | "your Python env" |
| `~/Projects/.memory-sweep/` | "your memory-ops directory" |
| `/home/<someone>/.dsh` | `$DSH_HOME` |
| a provider/model id | `list_subagent_models({ provider })` + the roster as of a dated observation |

Use real environment variables (`$DSH_HOME`, `$DSH_PROFILE_DIR`) where they exist — they are
portable *and* precise. Use an angle-bracket placeholder where no variable exists.

## Keep the evidence, drop the coordinates

An incident's **numbers and error strings** are the value of a skill — "41% of a 940-doc store was
cross-session duplication", "critical fired at >90% against a 90.5→92.3→92.0 plateau",
`patched (no change)`. Those are portable facts about how a class of system fails. Keep them.

What must go is the **address** of the machine it happened on. A teammate cannot act on your
kubeconfig path, and a path that is wrong for them reads as noise or, worse, as a default.

## Why not keep a labelled example section

Because a labelled example still gets copied. The old convention here was a fenced
"Reference instantiation — re-derive locally" tail; in practice it accreted personal values
(usernames in namespaces, a private dataset name, a private conda env) and turned into the thing
people copy first. If a detail is genuinely shared — the fleet's `formatter.sh`, the team's chart
conventions, a gateway tool's real name — state it as a fact in the body, because everyone must
use the same one.

**Your own values belong in your personal skill directory**, not here. Keep the same skill in both
places if you like: the shared copy carries the method and the placeholders, your personal copy
carries your paths.

## Checklist before adding or editing a skill here

- Would every concrete value in it be correct on a teammate's machine? If not, replace it.
- Does it name a dataset, store, namespace, env, or path that is tied to one person? Replace it.
- Do all cross-referenced skills actually exist in this directory? A `see <skill>` pointer to a
  skill that is not here is a dead end for everyone but its author.
- Does the method still stand on its own with every specific stripped? If not, it is a runbook for
  one operator's machine, and it belongs in a personal skill directory instead.
