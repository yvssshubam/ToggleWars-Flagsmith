"""Show where each Flagsmith setting comes from, without printing the secret."""

import os

from dotenv import dotenv_values

file_vals = dotenv_values(".env")
print(".env found:", os.path.exists(".env"))

for name in ("FS_AUTH_SCHEME", "FS_PROJECT_ID", "FS_ENV_KEY"):
    print(f"{name}: shell={os.environ.get(name)!r}  .env={file_vals.get(name)!r}")

for source, token in (("shell", os.environ.get("FS_TOKEN")), (".env", file_vals.get("FS_TOKEN"))):
    if not token:
        print(f"FS_TOKEN in {source}: not set")
        continue
    has_space_or_quote = any(c in token for c in (" ", '"', "'"))
    print(
        f"FS_TOKEN in {source}: length={len(token)}, "
        f"spaces/quotes={has_space_or_quote}, "
        f"starts with 'Api-Key' or 'Token'={token.startswith(('Api-Key', 'Token'))}, "
        f"contains a dot={'.' in token}"
    )