"""Run one worker: one collector, one SQLite writer, one dashboard."""
import argparse
import uvicorn

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    uvicorn.run("main:app", host="127.0.0.1", port=args.port, workers=1, log_level="info")
