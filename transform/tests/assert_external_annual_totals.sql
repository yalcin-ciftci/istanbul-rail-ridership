with totals as (
    select year, operator, measure,
        count(*) filter (where line_code is null) as system_rows,
        max(value) filter (where line_code is null) as system_value,
        sum(value) filter (where line_code is not null) as line_value
    from {{ ref('external_annual_totals') }}
    group by year, operator, measure
), duplicates as (
    select year, operator, measure, line_code
    from {{ ref('external_annual_totals') }}
    group by year, operator, measure, line_code
    having count(*) > 1
)
select year, operator, measure, 'total_mismatch' as failure
from totals
where system_rows <> 1 or line_value is null or line_value <> system_value
union all
select year, operator, measure, 'duplicate_grain' as failure
from duplicates
