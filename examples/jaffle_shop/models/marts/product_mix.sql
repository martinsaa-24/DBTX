select
    product_name,
    product_type,
    count(*) as units_sold,
    sum(product_price) as revenue
from {{ ref('int_order_items_priced') }}
group by 1, 2
