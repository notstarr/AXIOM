"""Graph persistence and read models.

The package intentionally does not re-export services.  Eager service imports
make table modules import the task module, which in turn imports graph during
composition, creating a circular import at application startup.
"""
