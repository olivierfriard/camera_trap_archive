"""Shared validation for optional sighting fields."""

PUP_NUMBER_ERROR = 'Il numero di cuccioli deve essere un intero positivo o zero, oppure vuoto.'


def parse_pup_number(value):
    """Return a nullable nonnegative PostgreSQL INTEGER, preserving unknown counts."""
    value = (value or '').strip()
    if not value:
        return None
    try:
        number = int(value)
        if not 0 <= number <= 2147483647:
            raise ValueError
    except ValueError:
        raise ValueError(PUP_NUMBER_ERROR) from None
    return number
