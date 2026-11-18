{{ config(severity='error') }}

with checked as (
    select
        start_date, end_date, event_type, label_tr, scope, partial_day, source_url,
        count(*) over (partition by start_date, end_date, event_type, label_tr) as duplicate_count
    from {{ ref('calendar_events') }}
)
select start_date, end_date, event_type, label_tr, scope, partial_day, source_url
from checked
where start_date > end_date
    or source_url not like 'https://%'
    or duplicate_count > 1
