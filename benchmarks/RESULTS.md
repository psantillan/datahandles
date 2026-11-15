# Context efficiency: measured results

Made by `benchmarks/context_efficiency.py` on 2026-11-15. CPU: Intel(R) Xeon(R) Gold 6136 CPU @ 3.00GHz. Python 3.12.3, DuckDB 2.0.0. One run for each size. Each file was written a moment before the run, so it is in the page cache.

Question: which three countries have the most purchase revenue? (filter, join, aggregate, sort, bounded read)

| Event rows | Parquet file | Rows as JSON | Protocol bytes to caller | Answer bytes | Time |
|---:|---:|---:|---:|---:|---:|
| 10,000 | 139,982 B | 877,653 B | 3,825 B | 147 B | 0.07 s |
| 100,000 | 1,200,186 B | 8,879,634 B | 3,828 B | 147 B | 0.074 s |
| 1,000,000 | 11,789,714 B | 89,794,093 B | 3,826 B | 146 B | 0.083 s |
| 10,000,000 | 117,633,508 B | 907,936,490 B | 3,832 B | 147 B | 0.13 s |
| 100,000,000 | 1,175,660,937 B | 9,179,443,674 B | 3,835 B | 146 B | 1.29 s |

- Protocol bytes = the JSON size of all five `OperationResult` objects, with the four derived handle summaries. The 9-byte change from the first row to the last row is more digits in the file size and in the time fields.
- Rows as JSON = the size of the events table from `COPY ... (FORMAT json)`. A caller that computes the answer itself must receive this much.
- The answer of each run is checked against one direct DuckDB SQL statement.
