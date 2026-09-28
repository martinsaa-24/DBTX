select
    id as order_item_id,
    order_id,
    sku as product_id
from {{ ref('raw_items') }}
