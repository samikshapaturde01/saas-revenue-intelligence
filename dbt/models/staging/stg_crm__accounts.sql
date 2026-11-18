-- One row per account from the most recent CRM snapshot.
-- Fixes: stale duplicate versions, inconsistent country codes, missing industry.
with source as (
    select * from {{ source('raw', 'accounts') }}
),

latest_snapshot as (
    select * from source
    where _source_file = (select max(_source_file) from source)
),

deduplicated as (
    select
        *,
        row_number() over (
            partition by account_id
            order by cast(updated_at as timestamp) desc
        ) as version_rank
    from latest_snapshot
)

select
    account_id,
    trim(company_name)                                   as company_name,
    coalesce(nullif(trim(industry), ''), 'Unknown')      as industry,
    company_size,
    case company_size
        when '1-10' then 1
        when '11-50' then 2
        when '51-200' then 3
        when '201-1000' then 4
        when '1000+' then 5
    end                                                  as company_size_rank,
    upper(trim(country))                                 as country_code,
    acquisition_channel,
    cast(created_at as timestamp)                        as created_at,
    cast(cast(created_at as timestamp) as date)          as signup_date,
    cast(updated_at as timestamp)                        as updated_at
from deduplicated
where version_rank = 1
