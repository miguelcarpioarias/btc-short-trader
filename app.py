import os
import threading
import dash
import dash_bootstrap_components as dbc
from dash import dcc, html, Input, Output, State, callback_context, dash_table
import plotly.graph_objs as go
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from apscheduler.schedulers.background import BackgroundScheduler
import logging
import yfinance as yf

# Alpaca clients
from alpaca.trading.client import TradingClient
from alpaca.trading.stream import TradingStream
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

# --- Configuration ---
API_KEY    = os.getenv('ALPACA_KEY') or os.getenv('ALPACA_API_KEY') or "PK93LZQTSB35L3CL60V5"
API_SECRET = os.getenv('ALPACA_SECRET') or os.getenv('ALPACA_SECRET_KEY') or "HDn7c1Mp3JVvgq98dphRDJH1nt3She3pe5Y9bJi0"
trade_client = TradingClient(API_KEY, API_SECRET, paper=True)

# Symbols
YF_SYMBOL    = 'BTC-USD'   # yfinance ticker
ALPACA_SYMBOL = 'BTC/USD'  # Alpaca trading symbol

# Set up logging
logging.basicConfig(level=logging.INFO)

# --- Streaming Order Updates ---
trade_updates_list = []
async def trade_updates_handler(update):
    try:
        event = update.event
        order = update.order
        trade_updates_list.append({
            'event': event,
            'symbol': order.symbol,
            'filled_qty': order.filled_qty,
            'filled_avg_price': order.filled_avg_price,
            'timestamp': update.timestamp
        })
    except Exception as e:
        logging.error(f"Stream handler error: {e}")

def start_trade_stream():
    stream = TradingStream(API_KEY, API_SECRET, paper=True)
    stream.subscribe_trade_updates(trade_updates_handler)
    stream.run()

threading.Thread(target=start_trade_stream, daemon=True).start()

# --- RSI Computation & Trading Job ---
def compute_rsi(series, window=14):
    delta = series.diff().dropna()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window).mean()
    avg_loss = loss.rolling(window).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    return 100 - (100 / (1 + rs))


def rsi_trading_job():
    try:
        # Fetch last 60 one-minute bars via yfinance
        ticker = yf.Ticker(YF_SYMBOL)
        df = ticker.history(interval="1m", period="1d", auto_adjust=True)
        df.index = pd.to_datetime(df.index)
        if df.index.tzinfo is None:
            df.index = df.index.tz_localize('UTC').tz_convert('America/New_York')
        else:
            df.index = df.index.tz_convert('America/New_York')
        df['Close'] = df['Close'].astype(float)
        df['RSI'] = compute_rsi(df['Close'], window=14)
        last_rsi = df['RSI'].iloc[-1]
        logging.info(f"RSI(1m)={last_rsi:.2f}")

        # Determine flat vs in-position
        positions = trade_client.get_all_positions()
        # Normalize symbol for comparison (e.g. 'BTC/USD' -> 'BTCUSD')
        clean_symbol = ALPACA_SYMBOL.replace('/', '')
        in_pos = any(p.symbol == clean_symbol and float(p.qty) > 0 for p in positions)(p.symbol == ALPACA_SYMBOL and float(p.qty) > 0 for p in positions)

        # Buy signal
        if last_rsi <= 30 and not in_pos:
            logging.info("RSI <=30; sending market BUY 0.5 BTC")
            mo = MarketOrderRequest(
                symbol=ALPACA_SYMBOL,
                side=OrderSide.BUY,
                type=OrderType.MARKET,
                time_in_force=TimeInForce.GTC,
                qty=0.5
            )
            trade_client.submit_order(order_data=mo)
        # Sell signal
        elif last_rsi >= 70 and in_pos:
            logging.info("RSI >=70; sending market SELL 0.5 BTC")
            mo = MarketOrderRequest(
                symbol=ALPACA_SYMBOL,
                side=OrderSide.SELL,
                type=OrderType.MARKET,
                time_in_force=TimeInForce.GTC,
                qty=0.5
            )
            trade_client.submit_order(order_data=mo)
        else:
            logging.info("No RSI trade signal or already in correct state.")
    except Exception as e:
        logging.error(f"RSI trading job error: {e}")

# Schedule RSI job every minute
scheduler = BackgroundScheduler(timezone='US/Eastern')
scheduler.add_job(rsi_trading_job, 'interval', minutes=1)
scheduler.start()

# --- Dash App Setup ---
app = dash.Dash(__name__, external_stylesheets=[dbc.themes.DARKLY])
server = app.server
brand_colors = {'background':'#2C3E50', 'text':'#ECF0F1'}

app.layout = dbc.Container([
    html.H2('Crypto Dashboard (BTC/USD)', style={'color':brand_colors['text']}),
    dbc.Row([
        dbc.Col([
            html.Label('BTC Qty', style={'color':brand_colors['text']}),
            dcc.Input(id='btc-qty', type='number', value=0.5, step=0.1), html.Br(), html.Br(),
            dbc.Button('Buy', id='buy-btc', color='success', className='me-2'),
            dbc.Button('Sell', id='sell-btc', color='danger'), html.Br(), html.Br(),
            html.Div(id='order-status', style={'color':brand_colors['text']})
        ], width=3),
        dbc.Col(dcc.Graph(id='price-chart'), width=9)
    ]),
    dbc.Row(dcc.Graph(id='rsi-chart'), className='mt-4'),
    dcc.Interval(id='interval', interval=30*1000, n_intervals=0),
    dbc.Row(dbc.Col(dash_table.DataTable(
        id='positions-table', page_size=10,
        style_header={'backgroundColor':brand_colors['background'],'color':brand_colors['text']},
        style_cell={'backgroundColor':brand_colors['background'],'color':brand_colors['text']}
    )), className='mt-4'),
    html.H4('Order Stream', style={'color':brand_colors['text'],'marginTop':'20px'}),
    dbc.Row(dbc.Col(dash_table.DataTable(
        id='orders-table', page_size=10,
        style_header={'backgroundColor':brand_colors['background'],'color':brand_colors['text']},
        style_cell={'backgroundColor':brand_colors['background'],'color':brand_colors['text']}
    )), className='mt-2')
], fluid=True, style={'backgroundColor':brand_colors['background'],'padding':'20px'})

# --- Callbacks ---
@app.callback(
    Output('price-chart','figure'), Input('interval','n_intervals')
)
def update_price(n):
    ticker = yf.Ticker(YF_SYMBOL)
    df = ticker.history(interval="1m", period="1d", auto_adjust=True)
    df.index = pd.to_datetime(df.index)
    if df.index.tzinfo is None:
        df.index = df.index.tz_localize('UTC').tz_convert('America/New_York')
    else:
        df.index = df.index.tz_convert('America/New_York')
    df = df.reset_index()
    fig = go.Figure(data=[
        go.Candlestick(
            x=df['Datetime'] if 'Datetime' in df.columns else df['Date'],
            open=df['Open'], high=df['High'], low=df['Low'], close=df['Close'],
            increasing_line_color='green', decreasing_line_color='red'
        )
    ])
    fig.update_layout(
        paper_bgcolor=brand_colors['background'], plot_bgcolor=brand_colors['background'],
        font_color=brand_colors['text'], xaxis_rangeslider_visible=False
    )
    return fig

@app.callback(
    Output('rsi-chart','figure'), Input('interval','n_intervals')
)
def update_rsi_chart(n):
    ticker = yf.Ticker(YF_SYMBOL)
    df = ticker.history(interval="1m", period="1d", auto_adjust=True)
    df.index = pd.to_datetime(df.index)
    if df.index.tzinfo is None:
        df.index = df.index.tz_localize('UTC').tz_convert('America/New_York')
    else:
        df.index = df.index.tz_convert('America/New_York')
    df = df.reset_index()
    df['Close'] = df['Close'].astype(float)
    df['RSI'] = compute_rsi(df['Close'], window=14)
    fig = go.Figure(data=[
        go.Scatter(
            x=df['Datetime'] if 'Datetime' in df.columns else df['Date'],
            y=df['RSI'], mode='lines', name='RSI'
        )
    ])
    fig.update_layout(
        paper_bgcolor=brand_colors['background'], plot_bgcolor=brand_colors['background'],
        font_color=brand_colors['text'], yaxis=dict(range=[0,100])
    )
    return fig

@app.callback(
    Output('order-status','children'),
    Input('buy-btc','n_clicks'), Input('sell-btc','n_clicks'), State('btc-qty','value')
)
def execute_order(buy, sell, qty):
    ctx = callback_context.triggered_id
    if not ctx:
        return ''
    side = OrderSide.BUY if ctx=='buy-btc' else OrderSide.SELL
    mo = MarketOrderRequest(
        symbol=ALPACA_SYMBOL, side=side, type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC, qty=qty
    )
    try:
        resp = trade_client.submit_order(order_data=mo)
        return f"✅ Order {resp.id} submitted (filled_qty={resp.filled_qty})"
    except Exception as e:
        return f"❌ Order failed: {e}"

@app.callback(
    Output('positions-table','data'), Output('positions-table','columns'),
    Input('interval','n_intervals')
)
def update_positions(n):
    rows = []
    try:
        pos = trade_client.get_all_positions() or []
        for p in pos:
            if p.symbol == ALPACA_SYMBOL:
                rows.append({
                    'Symbol': p.symbol,
                    'Qty': p.qty,
                    'Unrealized P/L': p.unrealized_pl,
                    'Market Value': p.market_value
                })
    except Exception:
        rows = []
    if not rows:
        rows = [{'Symbol':'None','Qty':0,'Unrealized P/L':0,'Market Value':0}]
    cols = [{'name':c,'id':c} for c in rows[0].keys()]
    return rows, cols

@app.callback(
    Output('orders-table','data'), Output('orders-table','columns'),
    Input('interval','n_intervals')
)
def update_orders(n):
    data = trade_updates_list[-20:]
    rows = data or [{'event':'None','symbol':'','filled_qty':0,'filled_avg_price':0,'timestamp':''}]
    cols = [{'name':k,'id':k} for k in rows[0].keys()]
    return rows, cols

if __name__=='__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
