from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.app.models.entities import Product,Supplier
class SupplierService:
    def __init__(self,db:Session): self.db=db
    def choose(self,product:Product):
        suppliers=self.db.scalars(select(Supplier).where(Supplier.is_active==True)).all()
        if not suppliers:return None
        preferred=[s for s in suppliers if s.supplier_id==product.preferred_supplier_id]
        if preferred:return preferred[0]
        return sorted(suppliers,key=lambda s:(-s.reliability_score,s.lead_time_days,s.min_order_value))[0]
