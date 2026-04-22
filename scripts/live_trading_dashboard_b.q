#!/usr/bin/env q
/ Version B dashboard in kdb/q+
/ Live Binance polling + HJB-style quote simulation + terminal dashboard.
/ This is independent from the Python dashboard.
/
/ Usage example:
/ q scripts/live_trading_dashboard_b.q -symbol BTCUSDT -pollms 150 -fillprob 0.35 -window 240 -log data/dashboard_b_events.jsonl

/ ---------- Argument Parsing ----------
parseArgs:{[x]
  d:`symbol`pollms`fillprob`window`log!("BTCUSDT";150;0.35;240;"")
  n:count x;
  i:0;
  while[i<n;
    a:x i;
    if["-symbol"~a; if[i+1<n; d[`symbol]:x i+1; i+:1]];
    if["-pollms"~a; if[i+1<n; d[`pollms]:"j"$x i+1; i+:1]];
    if["-fillprob"~a; if[i+1<n; d[`fillprob]:"f"$x i+1; i+:1]];
    if["-window"~a; if[i+1<n; d[`window]:"j"$x i+1; i+:1]];
    if["-log"~a; if[i+1<n; d[`log]:x i+1; i+:1]];
    i+:1;
  ];
  d[`fillprob]:max[0f;min[1f;d`fillprob]];
  d
 };
cfg:parseArgs .z.x;

/ ---------- Utilities ----------
nsToUs:{[ns] ("f"$ns)%1000f };
unixMsToTs:{[ms] 1970.01.01D00:00:00 + 0D00:00:00.001*ms};
nowTs:{ .z.p };

getJson:{[url]
  / .Q.hg returns bytes; .j.k parses JSON
  .j.k .Q.hg url
 };

logEvent:{[evt]
  if[count cfg`log;
    / emit compact JSON line
    .Q.dpft[hsym cfg`log;();{x,y}] enlist raze string .j.j evt,"\n";
  ];
 };

/ ---------- State ----------
window:cfg`window;
symbol:upper cfg`symbol;
pollMs:cfg`pollms;
fillProb:cfg`fillprob;

ticks:([] ts:`timestamp$(); sourceTs:`timestamp$(); recvTs:`timestamp$(); symbol:`symbol$(); bid:`float$(); ask:`float$(); mid:`float$(); sourceLagUs:`float$(); dashLagUs:`float$());
quotes:([] ts:`timestamp$(); quoteId:`long$(); mid:`float$(); qbid:`float$(); qask:`float$(); computeLagUs:`float$(); latencyCycles:`long$());
execs:([] ts:`timestamp$(); quoteId:`long$(); side:`symbol$(); px:`float$(); execLatencyUs:`float$(); e2eUs:`float$());

quoteId:0j;
inv:0j;
volHist:() ;

/ ---------- HJB-Style Approximation ----------
/ In q version B we do HJB-style approximation for speed and portability.
/ If you want strict HDL timing here, keep Python HJB backend as source-of-truth.
hjbQuote:{[mid;inv;vol]
  / Approximation similar to current RTL simplification
  reservation:mid - (inv * 0.001);
  spread:max[0.01;mid*0.0078];
  (reservation - spread*0.5; reservation + spread*0.5)
 };

calcVol:{[mids]
  if[count mids<3; :0.3f];
  p0:mids _ 1;
  p1:1_ mids;
  rets:log p1%p0;
  if[count rets<2; :0.3f];
  mu:avg rets;
  sdev:sqrt avg (rets-mu)*(rets-mu);
  min[2f;max[0.01f;sdev*sqrt 31536000f]]
 };

/ ---------- Data Fetch ----------
fetchTick:{[]
  srv:getJson "https://api.binance.com/api/v3/time";
  bt:getJson "https://api.binance.com/api/v3/ticker/bookTicker?symbol=",symbol;
  t1:nowTs[];

  srcTs:unixMsToTs "j"$srv`serverTime;
  bid:"f"$bt`bidPrice;
  ask:"f"$bt`askPrice;
  mid:(bid+ask)%2f;

  sLag:nsToUs long t1-srcTs;
  dLag:0f;
  `ts`sourceTs`recvTs`symbol`bid`ask`mid`sourceLagUs`dashLagUs!(t1;srcTs;t1;`$symbol;bid;ask;mid;sLag;dLag)
 };

/ ---------- Dashboard ----------
render:{[]
  system "clear";
  -1 "VeriTrade Dashboard B (kdb/q+)";
  -1 "feed: REAL (Binance REST polling)";
  -1 "compute: SIMULATED (HJB-style in q)";
  -1 "execution: SIMULATED fill model";
  -1 "------------------------------------------------------------";

  if[count ticks;
    lt:last ticks;
    -1 "symbol: ",string lt`symbol;
    -1 "mid/bid/ask: ",string lt`mid," / ",string lt`bid," / ",string lt`ask;
    -1 "source lag (us): ",string lt`sourceLagUs;
  ];

  cq:count quotes;
  ce:count execs;
  -1 "quotes: ",string cq,"    executions: ",string ce;

  if[cq>0;
    lq:last quotes;
    -1 "quote id: ",string lq`quoteId,"   qbid/qask: ",string lq`qbid," / ",string lq`qask;
    -1 "compute lag (us): ",string lq`computeLagUs,"   latency cycles: ",string lq`latencyCycles;
  ];

  if[ce>0;
    le:last execs;
    -1 "last exec: ",string le`side," @ ",string le`px;
    -1 "exec latency (us): ",string le`execLatencyUs,"   end-to-end (us): ",string le`e2eUs;
  ];

  -1 "------------------------------------------------------------";
  -1 "Ctrl+C to stop";
 };

/ ---------- Main Loop ----------
while[1b;
  tick:fetchTick[];

  / append tick (rolling)
  ticks,:enlist tick;
  if[count ticks>window; ticks:window#(-window)_ticks];

  / update vol + quote
  volHist,:tick`mid;
  if[count volHist>window; volHist:(-window)_volHist];
  vol:calcVol volHist;

  qStart:nowTs[];
  qbqa:hjbQuote[tick`mid;inv;vol];
  qEnd:nowTs[];

  quoteId+:1;
  quote:`ts`quoteId`mid`qbid`qask`computeLagUs`latencyCycles!(qEnd;quoteId;tick`mid;first qbqa;last qbqa;nsToUs long qEnd-qStart;4j);
  quotes,:enlist quote;
  if[count quotes>window; quotes:window#(-window)_quotes];

  / fill model: probabilistic maker fills to keep activity visible
  if[("f"$rand 1f) <= fillProb;
    side:$[(quoteId mod 2)=0;`BUY;`SELL];
    px:$[side=`BUY; tick`ask; tick`bid];
    e2e:nsToUs long (qEnd - tick`sourceTs);
    ex:`ts`quoteId`side`px`execLatencyUs`e2eUs!(qEnd;quoteId;side;px;16f;e2e);
    execs,:enlist ex;
    if[count execs>window; execs:window#(-window)_execs];
    / toy inventory update
    inv+:$[side=`BUY;1j;-1j];
    logEvent `kind`ts`symbol`side`price`sourceLagUs`computeLagUs`e2eUs!("execution";string qEnd;symbol;string side;px;tick`sourceLagUs;quote`computeLagUs;e2e);
  ];

  logEvent `kind`ts`symbol`mid`bid`ask`sourceLagUs`computeLagUs!("quote";string qEnd;symbol;tick`mid;quote`qbid;quote`qask;tick`sourceLagUs;quote`computeLagUs);
  render[];
  system "sleep ",string 0.001*pollMs;
};
