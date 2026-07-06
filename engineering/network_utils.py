import time
import random

# Model fallback chain for rate-limit / quota errors
PRIMARY_MODEL   = "gemini-2.5-flash"
FALLBACK_MODEL  = "gemini-3.1-flash-lite"

# Error substrings that indicate a quota / rate-limit condition
_QUOTA_SIGNALS = [
    "429",
    "quota",
    "rate limit",
    "rate_limit",
    "resource_exhausted",
    "resourceexhausted",
    "toomanyrequests",
]

def generate_with_fallback(ai_client, contents, config):
    """
    Calls ai_client.models.generate_content() using PRIMARY_MODEL.
    If a rate-limit or quota error is detected, automatically retries
    once using FALLBACK_MODEL before giving up.

    Returns the response object, or raises the exception if both models fail.
    """
    for model in (PRIMARY_MODEL, FALLBACK_MODEL):
        try:
            response = ai_client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            if model != PRIMARY_MODEL:
                print(f"⚠️  [Fallback] Used {model} (primary quota exhausted).")
            return response
        except Exception as e:
            err_str = str(e).lower()
            is_quota_error = any(sig in err_str for sig in _QUOTA_SIGNALS)
            if model == PRIMARY_MODEL and is_quota_error:
                print(f"⚠️  [Fallback] {PRIMARY_MODEL} quota/rate-limit hit — switching to {FALLBACK_MODEL}.")
                continue  # retry with fallback
            raise  # non-quota error or fallback also failed → let caller handle it


def retry_with_backoff(func, max_retries=5, base_delay=2.0):
    """
    Executes a function and automatically retries on network failures using exponential backoff.
    """
    retries = 0
    while retries <= max_retries:
        try:
            # Attempt to execute the API call
            return func()
            
        except Exception as e:
            # Check if this is an API or Network error.
            # Using str(type(e)) allows us to catch google.genai errors without strict imports
            error_type = type(e).__name__
            # HTTPError: requests raise_for_status() on 429/5xx — transient for our sources.
            # TransientDataError: page fetched OK but structurally unusable (soft-block/captcha).
            if error_type in ["APIError", "ServerError", "ClientError", "ConnectionError", "TimeoutError", "ReadTimeout", "ConnectTimeout", "HTTPError", "TransientDataError"]:
                print(f"⚠️ Network Error: {error_type} - {e}")
                
                if retries == max_retries:
                    print("🔴 CRITICAL: Max retries reached. Aborting this target.")
                    return None # Graceful failure
                    
                # Calculate wait time: base_delay * 2^retries + jitter
                wait_time = (base_delay * (2 ** retries)) + random.uniform(0, 1)
                
                print(f"🔄 Retrying in {round(wait_time, 2)} seconds... (Attempt {retries + 1}/{max_retries})")
                time.sleep(wait_time)
                retries += 1
            else:
                # If it's a completely different error (like bad JSON or a missing variable), 
                # don't retry, just crash gracefully so you can fix the bug.
                print(f"❌ Fatal Code Error: {error_type} - {e}")
                return None
