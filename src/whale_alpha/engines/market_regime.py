from __future__ import annotations
from dataclasses import dataclass
from statistics import median
from whale_alpha.integrations.token_hunter_market import TokenMarketSnapshot

@dataclass(frozen=True)
class MarketRegime:
    name: str
    score: float
    breadth_pct: float
    median_price_change_1h_pct: float
    median_volume_acceleration: float
    median_buy_ratio: float
    median_liquidity_ratio: float
    trend: str
    reasons: tuple[str, ...]

def _volume_acceleration(s: TokenMarketSnapshot) -> float:
    short=max(s.volume_5m_usd,0.0)/5.0; long=max(s.volume_1h_usd,0.0)/60.0
    return short/long if long>0 else (2.0 if short>0 else 0.0)

def _buy_ratio(s: TokenMarketSnapshot)->float:
    total=s.buys_5m+s.sells_5m; return s.buys_5m/total if total else 0.5

def _liquidity_ratio(s: TokenMarketSnapshot)->float:
    return max(s.liquidity_usd or 0.0,0.0)/s.market_cap_usd if s.market_cap_usd and s.market_cap_usd>0 else 0.0

def classify_market_regime(snapshots:list[TokenMarketSnapshot])->MarketRegime:
    usable=[s for s in snapshots if s.volume_1h_usd>0 and s.buys_5m+s.sells_5m>0]
    if not usable: return MarketRegime('UNKNOWN',0,0,0,0,0.5,0,'UNKNOWN',('INSUFFICIENT_MARKET_DATA',))
    breadth=sum(1 for s in usable if s.price_change_1h_pct>0)/len(usable)*100
    med_change=median(s.price_change_1h_pct for s in usable); med_vol=median(_volume_acceleration(s) for s in usable); med_buy=median(_buy_ratio(s) for s in usable); med_liq=median(_liquidity_ratio(s) for s in usable)
    score=max(0,min(100,50+(breadth-50)*.55+max(-20,min(20,med_change*1.5))+max(-12,min(12,(med_buy-.5)*120))+max(-8,min(8,(med_vol-1)*10))))
    if breadth>=65 and med_change>=3 and med_buy>=.55 and med_vol>=1: name='RISK_ON'
    elif breadth>=55 and med_change>=0 and med_buy>=.52: name='BULLISH'
    elif breadth<=30 and med_change<=-5 and med_buy<.45: name='PANIC'
    elif breadth<=42 and med_change<-1 and med_buy<.48: name='RISK_OFF'
    else: name='NEUTRAL'
    if med_change>=5 and med_vol>=1.2: trend='STRONG_UPTREND'
    elif med_change>1 and med_vol>=.9: trend='UPTREND'
    elif med_change<=-5 and med_vol>=1.2: trend='STRONG_DOWNTREND'
    elif med_change<-1: trend='DOWNTREND'
    else: trend='RANGE'
    reasons=[]
    if breadth>=60: reasons.append('POSITIVE_MARKET_BREADTH')
    elif breadth<=40: reasons.append('WEAK_MARKET_BREADTH')
    if med_buy>=.55: reasons.append('BUY_SIDE_DOMINANCE')
    elif med_buy<.48: reasons.append('SELL_SIDE_DOMINANCE')
    if med_vol>=1.2: reasons.append('VOLUME_EXPANSION')
    if med_liq>=.05: reasons.append('HEALTHY_LIQUIDITY_BASE')
    return MarketRegime(name,round(score,2),round(breadth,2),round(med_change,2),round(med_vol,2),round(med_buy,4),round(med_liq,4),trend,tuple(reasons))

def market_regime_gate(snapshot:TokenMarketSnapshot, regime:MarketRegime, *, score:float, severe_flags:set[str], risk_off_min_score:float=88, neutral_min_score:float=84, risk_on_min_score:float=80)->tuple[bool,tuple[str,...]]:
    flags=[]; buy=_buy_ratio(snapshot); liq=_liquidity_ratio(snapshot); vol=_volume_acceleration(snapshot)
    if regime.name in {'PANIC','RISK_OFF'}:
        if regime.name=='PANIC': flags.append('MARKET_PANIC')
        if score < risk_off_min_score: flags.append('MARKET_REGIME_SCORE_TOO_LOW')
        if buy<.56: flags.append('WEAK_BUY_SIDE_IN_RISK_OFF')
        if snapshot.price_change_1h_pct<=0: flags.append('TOKEN_TREND_NOT_POSITIVE')
        if liq<.05: flags.append('LIQUIDITY_TOO_THIN_FOR_RISK_OFF')
        if vol<1: flags.append('NO_VOLUME_EXPANSION')
    elif regime.name=='NEUTRAL':
        if score<neutral_min_score: flags.append('MARKET_REGIME_SCORE_TOO_LOW')
        if buy<.53: flags.append('BUY_SIDE_NOT_CONFIRMED')
        if snapshot.price_change_1h_pct<1: flags.append('TREND_NOT_CONFIRMED')
        if liq<.04: flags.append('LIQUIDITY_TOO_THIN')
    else:
        if score<risk_on_min_score: flags.append('MARKET_REGIME_SCORE_TOO_LOW')
        if buy<.51: flags.append('BUY_SIDE_NOT_CONFIRMED')
        if snapshot.price_change_1h_pct<-2: flags.append('NEGATIVE_TOKEN_TREND')
        if liq<.03: flags.append('LIQUIDITY_TOO_THIN')
    if severe_flags: flags.append('FUNDAMENTAL_SAFETY_FAILURE')
    return not flags,tuple(dict.fromkeys(flags))
