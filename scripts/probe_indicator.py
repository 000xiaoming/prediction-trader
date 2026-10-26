import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8080/api/state", timeout=20) as response:
    print(response.read().decode())
