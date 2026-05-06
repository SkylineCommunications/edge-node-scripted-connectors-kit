import requests

import argparse
import datetime
import sys

def main(data_endpoint: str, run_secret: str) -> None:
    """
    Posts a simple hello-world JSON payload with a timestamp to validate edge node connectivity.
    
    Args:
        data_endpoint (str): The URL of the edge node data endpoint to post to.
        run_secret (str): The secret token for authenticating with the edge node.
    """
    headers = {
        "identifier": "hello-world",
        "type": "edge-node-hello-world",
        "runSecret": run_secret,
    }
    
    now = datetime.datetime.now()
    json = {"message": "Hello world!", "timestamp": now.isoformat()}
    response = requests.post(data_endpoint, headers=headers, json=json)

    print(f"Response status code: {response.status_code}")
    print(f"Response content: {response.content}")

if __name__ == "__main__":
    # Parse command-line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("--runSecret", required=True)
    args = parser.parse_args()

    run_secret = args.runSecret

    # Run the main function and handle any exceptions
    try:
        main("http://localhost:5016/api/data", run_secret)
    except Exception as e:
        print(f"An error occurred: {e}", file=sys.stderr)