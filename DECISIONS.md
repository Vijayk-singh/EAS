# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** `employees.emp_code` is unique for identity and lookups. `department, joined_on` and `joined_on` support headcount and trend eligibility with or without department filters. Attendance uses the unique natural key `emp_code, date`, plus date/employee and employee/date compound indexes for ordered pagination, monthly analytics, and joins. A standalone `status` index was omitted due to low selectivity.
2. **Punch-in race.** Concurrent requests derive the same natural key and attempt an insert without check-then-insert. MongoDB accepts the first insert under the unique index; competing inserts fail with duplicate-key errors, returning HTTP 409. This guarantees correctness across processes.
3. **Ties.** The late leaderboard uses `$rank` rather than array offsets, giving tied totals identical ranks. Filtering `rank <= limit` returns all tied employees at the cutoff, sorted deterministically by employee code.
4. **Headcount.** The department aggregation starts from employees who joined on or before month end, left-joining attendance. This preserves employees with zero attendance records and counts each eligible worker once.
5. **At 100x scale**, I would partition older attendance into monthly collections, precompute daily rollups, and route analytics to secondary replicas.
