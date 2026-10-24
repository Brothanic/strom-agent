# Frozen fixture days

Verbatim DE-LU day-ahead price responses from the
[Energy-Charts API](https://api.energy-charts.info/) (Fraunhofer ISE), recorded
once with `scripts/record_fixtures.py`. Request URLs, fetch times and the
license string of each response are in `provenance.json`. Tests and evals read
only these files, so their correct answers never change. Do not re-record them.

Data license: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/),
Bundesnetzagentur | SMARD.de.

| Day | Resolution | Why it is included |
|---|---|---|
| 2025-09-30 | 60 min | Last recorded hourly day before the 15-minute market |
| 2025-10-01 | 15 min | First 15-minute day; comparison example in the spec |
| 2025-10-08 | 15 min | Second day of the spec's comparison example |
| 2025-10-26 | 15 min | DST ends: 25-hour day with 02:00 occurring twice |
| 2026-03-29 | 15 min | DST starts: 23-hour day without 02:00 |
| 2026-04-05 | 15 min | Easter Sunday with many negative prices |
| 2026-10-14 | 15 min | Ordinary recent weekday |
