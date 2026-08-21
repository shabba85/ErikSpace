"""Pure functions over statement data.

Every function in this package takes a Company/Period and returns Q objects.
None of them fetch, none of them cache, none of them consult the clock, and
none of them substitute a default for a missing input.  That is what makes them
unit-testable in isolation and what makes two runs over unchanged data produce
byte-identical output.
"""
