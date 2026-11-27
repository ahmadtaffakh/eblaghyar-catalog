from __future__ import annotations

import argparse
import json

from .exchange import publish_request
from .ingest import scan_exchange, scan_historical
from .store import CatalogStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Eblaghyar intranet catalogue")
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan")
    scan.add_argument("--exchange-dir", required=True)
    scan.add_argument("--db", required=True)
    historical = sub.add_parser("import-historical")
    historical.add_argument("--source-root", required=True)
    historical.add_argument("--db", required=True)
    search = sub.add_parser("search")
    search.add_argument("--db", required=True)
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=50)
    request = sub.add_parser("request")
    request.add_argument("--exchange-dir", required=True)
    request.add_argument("--notice-no", required=True)
    args = parser.parse_args()
    if args.command == "scan":
        result = scan_exchange(args.exchange_dir, CatalogStore(args.db))
    elif args.command == "import-historical":
        result = scan_historical(args.source_root, CatalogStore(args.db))
    elif args.command == "search":
        result = CatalogStore(args.db).search(args.query, args.limit)
    else:
        result = {"request": str(publish_request(args.exchange_dir, args.notice_no))}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
