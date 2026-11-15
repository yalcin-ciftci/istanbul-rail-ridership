select ride_date, line_name, count(*) as row_count
from {{ ref('fct_line_daily') }}
group by ride_date, line_name
having count(*) > 1
