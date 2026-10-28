"""Minimal stub for allapps utils."""
def fmt_short(n):
    try:
        return f"₹{float(n):.2f}"
    except:
        return str(n)
def fmt_curr(n):
    return fmt_short(n)
