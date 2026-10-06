import os
import sys


def main() -> None:
    os.environ["SEARCH_PROVIDER"] = "brightdata"
    from modules import search

    print(f"Zone: {os.getenv('BRIGHTDATA_ZONE', 'serp_api1')}")
    query = " ".join(sys.argv[1:]).strip() or "Pizza Hut"
    print(f"\nTesting Bright Data query: {query}")
    results = search._brightdata_text(query)
    print(f"Results: {len(results)}")
    for result in results[:5]:
        print("-", result.get("title", ""), result.get("href", ""))


if __name__ == "__main__":
    main()
