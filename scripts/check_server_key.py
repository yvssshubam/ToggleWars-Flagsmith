"""Read-only: which environment does demo_shop/.env's server key belong to,
and which flags does the shop's SDK see? Never prints the key.

    python -m scripts.check_server_key
"""

import logging
import os

from dotenv import load_dotenv
from flagsmith import Flagsmith

from demo_shop.app import shop_config

logging.basicConfig(level=logging.WARNING)

PRODUCTION_KEY = "YVBbF2u3wfxhGLS8StCbhU"
DEVELOPMENT_KEY = "JM8wUtydhVkCAb9SNF2zE9"


def main():
    load_dotenv()  # root .env, only to read FS_ENV_KEY for comparison
    server_key = shop_config()["FLAGSMITH_SERVER_KEY"]
    print(f"server key: starts with 'ser.' = {server_key.startswith('ser.')}, length = {len(server_key)}")

    sdk = Flagsmith(environment_key=server_key, enable_local_evaluation=True,
                    environment_refresh_interval_seconds=60)
    try:
        local = sdk._local_evaluation  # private; only for this diagnostic (flagsmith==6.7.0)
        if local is None:
            print("environment document: NOT loaded (bad key or network); see errors above")
            return
        env = local.context.get("environment", {})
        env_key = env.get("key")
        which = {PRODUCTION_KEY: "Production", DEVELOPMENT_KEY: "Development"}.get(env_key, "unknown")
        print(f"environment document: loaded, environment = {which} "
              f"(name in document: {env.get('name')!r})")
        print(f"bot's FS_ENV_KEY is {'Production' if os.environ.get('FS_ENV_KEY') == PRODUCTION_KEY else 'not Production'}")

        names = sorted(f.feature_name for f in sdk.get_environment_flags().all_flags())
        print(f"flags in the document: {names}")
        segments = local.context.get("segments") or {}
        print(f"segments in the document: {len(segments)}")
    finally:
        sdk.close()


if __name__ == "__main__":
    main()
