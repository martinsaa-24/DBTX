select
    id as store_id,
    name as store_name,
    cast(opened_at as timestamp) as opened_at,
    tax_rate
from {{ ref('raw_stores') }}
