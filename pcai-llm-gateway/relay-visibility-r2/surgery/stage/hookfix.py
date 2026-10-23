p='/var/dsh/bin/dsh-startup.sh'
s=open(p).read()
bad='# Wave R1 relay-render: re-apply patched files after every restart (idempotent, version-guarded)/var/dsh/data/relay-r2/relay-restore.sh'
good='# Wave R1 relay-render: re-apply patched files after every restart (idempotent, version-guarded)\n[ -x /var/dsh/data/relay-r2/relay-restore.sh ] && /var/dsh/data/relay-r2/relay-restore.sh\n'
if bad in s:
    open(p,'w').write(s.replace(bad, good))
    print('FIXED: hook split into comment + guarded invocation')
elif 'relay-restore.sh ] && /var/dsh/data/relay-r2/relay-restore.sh' in s:
    print('already correct')
else:
    print('pattern not found; current relay lines:')
    for l in s.splitlines():
        if 'relay' in l: print(' ', l)
