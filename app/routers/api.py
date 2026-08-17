from fastapi import APIRouter
from sqlalchemy import select

from app.database import async_session_factory
from app.models import Patch, Product

router = APIRouter(prefix="/api")


@router.get("/products")
async def list_products():
    async with async_session_factory() as session:
        products = (await session.execute(select(Product))).scalars().all()
        return [
            {
                "key": p.key,
                "display_name": p.display_name,
                "family": p.family,
                "is_ltsc": p.is_ltsc,
                "source_url": p.source_url,
                "support_end_date": p.support_end_date.isoformat() if p.support_end_date else None,
                "support_ended": p.support_ended,
            }
            for p in products
        ]


@router.get("/products/{product_key}/patches")
async def list_patches(product_key: str):
    async with async_session_factory() as session:
        product = (
            await session.execute(select(Product).where(Product.key == product_key))
        ).scalar_one_or_none()
        if product is None:
            return {"error": "not found"}
        patches = (
            await session.execute(
                select(Patch)
                .where(Patch.product_id == product.id)
                .order_by(Patch.release_date.desc().nullslast())
            )
        ).scalars().all()
        return [
            {
                "kb_number": pt.kb_number,
                "build": pt.build,
                "title": pt.title,
                "update_type": pt.update_type,
                "release_date": pt.release_date.isoformat() if pt.release_date else None,
                "severity": pt.severity,
                "kb_url": pt.kb_url,
                "source": pt.source,
            }
            for pt in patches
        ]
