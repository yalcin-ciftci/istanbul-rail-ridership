select month, line_code, count(*) as row_count
from {{ ref('fct_hourly_coverage_month') }}
group by month, line_code
having month is null or line_code is null or count(*) <> 1
