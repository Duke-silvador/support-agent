# API rate limits

Each API key is limited to **600 requests per minute** and 50,000 requests per day.
Bursts above 900 requests in any 10-second window are throttled.

## When you hit the limit

The API returns HTTP 429 with a `Retry-After` header in seconds. Exponential backoff
starting at one second is recommended.

## Increasing limits

Business plans can request a limit increase through the support portal; approval
usually takes one business day.
