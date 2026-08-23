from whale_alpha.engines.market_regime import classify_market_regime, market_regime_gate
from whale_alpha.integrations.token_hunter_market import TokenMarketSnapshot

def snap(i, change, buys, sells, vol5, vol1h, mc=100000, liq=10000):
    return TokenMarketSnapshot(f"m{i}","T","T",None,"raydium",None,1.0,mc,liq,vol5,vol1h,buys,sells,change/12,change,True)

def test_bullish_regime():
    r=classify_market_regime([snap(i,8,18,7,9000,50000) for i in range(8)])
    assert r.name in {"RISK_ON","BULLISH"}
    assert r.trend in {"STRONG_UPTREND","UPTREND"}

def test_panic_blocks_weak_token():
    ss=[snap(i,-8,4,14,4000,50000,liq=4000) for i in range(8)]
    r=classify_market_regime(ss); ok,flags=market_regime_gate(ss[0],r,score=90,severe_flags=set())
    assert not ok
    assert "MARKET_PANIC" in flags or "WEAK_BUY_SIDE_IN_RISK_OFF" in flags
