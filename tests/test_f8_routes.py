# -*- coding: utf-8 -*-
"""F8路由注册冒烟验证"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import main
routes = sorted({r.path for r in main.app.routes if "obligation" in getattr(r, "path", "")})
print("\n".join(routes))
assert "/api/contracts/{cid}/obligations" in routes
assert "/api/contracts/{cid}/obligations/extract" in routes
assert "/api/obligations/overview" in routes
assert "/api/obligations/export" in routes
assert "/api/obligations/scan" in routes
print("F8路由注册验证通过")
