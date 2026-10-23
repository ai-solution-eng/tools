lines = open("tests/test_title_requests.py", encoding="utf-8").read().split(chr(10))
new = open(".newblock.txt", encoding="utf-8").read().rstrip(chr(10)).split(chr(10))
start, end = 243, 280
print("first removed:", lines[start][:70])
print("last removed :", lines[end-1][:70])
out = lines[:start] + new + lines[end:]
open("tests/test_title_requests.py", "w", encoding="utf-8").write(chr(10).join(out))
print("spliced ok")