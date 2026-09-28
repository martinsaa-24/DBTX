-- Order total must equal subtotal plus tax, and subtotal must equal the sum of item prices.
select order_id
from {{ ref('orders') }}
where abs(order_total - (subtotal + tax_paid)) > 0.005
   or abs(subtotal - items_subtotal) > 0.005
