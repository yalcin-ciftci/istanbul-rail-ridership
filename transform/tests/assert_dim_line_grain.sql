select line_name, count(*) as row_count
from {{ ref('dim_line') }}
group by line_name
having count(*) > 1
