select
    sku as product_id,
    name as product_name,
    type as product_type,
    cast(price / 100.0 as decimal(12, 2)) as product_price
from {{ ref('raw_products') }}
