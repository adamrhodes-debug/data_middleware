import os, requests

# read .env from this folder
for line in open(".env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip()

sub = os.environ["PICKL_SUBDOMAIN"]
key = os.environ["PICKL_API_KEY"]
sec = os.environ["PICKL_SECRET"]

r = requests.get(
    f"https://{sub}.revelup.com/resources/Customer/",
    headers={"API-AUTHENTICATION": f"{key}:{sec}"},
    params={"limit": 1, "format": "json"},
    timeout=60,
)
data = r.json()

print("META:", data["meta"])
print()
print("FIELDS:")
for k in sorted(data["objects"][0].keys()):
    print("   ", k, "=", repr(data["objects"][0][k])[:70])
