import os
import requests
import datetime
import calendar
import time
import json

# Config from environment
USAGE_ENDPOINT = os.getenv("USAGE_ENDPOINT")
IBM_BASE_URL = os.getenv("IBM_BASE_URL")
RESOURCE_ID = os.getenv("RESOURCE_ID")
RESOURCE_INSTANCE_ID = os.getenv("RESOURCE_INSTANCE_ID")
PLAN_ID = os.getenv("PLAN_ID")
REGION = os.getenv("REGION")
MEASURE_NAME = "total_query_count"
RETRY_COUNT = 3
RETRY_DELAY = 5  # seconds

with open("/var/run/secrets/ibm/ibm_token", "r") as f:
    IBM_TOKEN = f.read().strip()

with open("/var/run/secrets/internal/usage_api_key", "r") as f:
    USAGE_API_KEY = f.read().strip()


def previous_month_dates():
    """Return previous month start/end as YYYY-MM-DD and epoch milliseconds"""
    now = datetime.datetime.utcnow()
    year = now.year
    month = now.month - 1 or 12
    if month == 12:
        year -= 1
    first_day = datetime.datetime(year, month, 1)
    last_day = datetime.datetime(year, month, calendar.monthrange(year, month)[1], 23, 59, 59)
    start_str = first_day.strftime("%Y-%m-%d")
    end_str = last_day.strftime("%Y-%m-%d")
    start_ms = int(first_day.timestamp() * 1000)
    end_ms = int(last_day.timestamp() * 1000)
    return start_str, end_str, start_ms, end_ms


def get_internal_usage(start, end):
    """Call internal API, retry if status != 200"""
    headers = {"apikey": USAGE_API_KEY, "Content-Type": "text/plain"}
    payload = {"start": start, "end": end}
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            resp = requests.post(USAGE_ENDPOINT, headers=headers, json=payload, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            print(f"[Attempt {attempt}] Internal API returned {resp.status_code}, retrying...")
        except Exception as e:
            print(f"[Attempt {attempt}] Internal API exception: {e}, retrying...")
        time.sleep(RETRY_DELAY)
    raise Exception("Failed to get internal usage after retries")


def send_usage_to_ibm(start_ms, end_ms, quantity):
    """Call IBM usage API, retry if status != 202"""
    body = [
        {
            "resource_instance_id": RESOURCE_INSTANCE_ID,
            "plan_id": PLAN_ID,
            "region": REGION,
            "start": start_ms,
            "end": end_ms,
            "measured_usage": [
                {"measure": MEASURE_NAME, "quantity": quantity}
            ]
        }
    ]
    headers = {
        "Authorization": f"Bearer {IBM_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    url = f"{IBM_BASE_URL}/v4/metering/resources/{RESOURCE_ID}/usage"

    for attempt in range(1, RETRY_COUNT + 1):
        try:
            resp = requests.put(url, headers=headers, json=body, timeout=15)
            if resp.status_code == 202:
                print("IBM metering accepted")
                return resp.json() if resp.text else {}
            print(f"[Attempt {attempt}] IBM API returned {resp.status_code}, retrying...")
        except Exception as e:
            print(f"[Attempt {attempt}] IBM API exception: {e}, retrying...")
        time.sleep(RETRY_DELAY)
    raise Exception("Failed to send usage to IBM after retries")


def main():
    start_str, end_str, start_ms, end_ms = previous_month_dates()
    print("Previous month range:", start_str, end_str)

    usage = get_internal_usage(start_str, end_str)
    total_queries = usage.get(MEASURE_NAME, 0)

    send_usage_to_ibm(start_ms, end_ms, total_queries)


if __name__ == "__main__":
    main()
