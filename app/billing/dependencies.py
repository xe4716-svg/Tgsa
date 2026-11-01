from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from app.auth.dependencies import AuthPrincipal, get_current_principal
from app.billing.service import FeatureAccessDenied, ensure_feature


def require_feature(feature_key: str):
    async def dependency(
        request: Request,
        principal: AuthPrincipal = Depends(get_current_principal),
    ) -> AuthPrincipal:
        async with request.app.state.db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)",
                    str(principal.tenant_id),
                )
                try:
                    await ensure_feature(conn, principal.tenant_id, feature_key)
                except FeatureAccessDenied:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail={
                            "code": "FEATURE_NOT_IN_PLAN",
                            "feature": feature_key,
                            "message": "This feature is not included in your current membership plan.",
                        },
                    )
        return principal

    return dependency
