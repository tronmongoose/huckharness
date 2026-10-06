def parse_pairs(text):
    """Parse lines of the form key=value into a dict."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("=")
        key = parts[0].strip()
        value = parts[1].strip()
        result[key] = value
    return result
