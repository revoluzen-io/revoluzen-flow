"""Reading files from mainframes and older business systems.

* ``codepages``: EBCDIC code pages (037, 1140, 1047, 500, 273 and others).
* ``records``: splitting a file into records (lines, fixed length, RDW, BDW).
* ``fixed_width``: fixed-width text into typed columns.
* ``copybook``: COBOL copybook records (packed and zoned decimals, binary,
  OCCURS, OCCURS DEPENDING ON, REDEFINES) into typed columns.

The File step (``nodes/file_node.py``) uses these for its ``fixed_width`` and
``copybook`` formats and for EBCDIC text. Everything is read in batches and
landed in DuckDB through Arrow, the same way as Avro.
"""
