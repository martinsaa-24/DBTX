select
    items.order_item_id,
    items.order_id,
    products.product_id,
    products.product_name,
    products.product_type,
    products.product_price
from {{ ref('stg_order_items') }} as items
join {{ ref('stg_products') }} as products using (product_id)
