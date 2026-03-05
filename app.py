import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import warnings
warnings.filterwarnings('ignore')

st.set_page_config(
    page_title="Ops Team Dashboard",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    .main { background-color: #0f1117; }
    .block-container { padding-top: 1.5rem; padding-bottom: 1rem; }
    .metric-card {
        background: #1a1d2e;
        border-radius: 12px;
        padding: 20px 24px;
        border: 1px solid #2d3250;
        text-align: center;
    }
    .metric-value { font-size: 2.2rem; font-weight: 800; margin: 0; }
    .metric-label { font-size: 0.85rem; color: #8b949e; margin-top: 4px; }
    .section-title {
        font-size: 1.1rem; font-weight: 700;
        color: #e6edf3; margin: 1.2rem 0 0.6rem 0;
        border-left: 3px solid #58a6ff; padding-left: 10px;
    }
    div[data-testid="stMetric"] { background: #1a1d2e; border-radius: 10px; padding: 12px 18px; border: 1px solid #2d3250; }
    div[data-testid="stMetric"] label { color: #8b949e !important; }
    div[data-testid="stMetric"] div { color: #e6edf3 !important; }
    .stDataFrame { background: #1a1d2e; }
    thead tr th { background-color: #2d3250 !important; color: #58a6ff !important; }
</style>
""", unsafe_allow_html=True)

# ── LOAD DATA ─────────────────────────────────────────────────────────────────
@st.cache_data
def load_data():
    logs = pd.read_excel("User Logs.xlsx")
    tasks = pd.read_excel("Ops Tasks.xlsx")

    logs['ts']   = pd.to_datetime(logs['Date (Local)'], errors='coerce')
    logs['date'] = logs['ts'].dt.date

    tasks['created_ts']  = pd.to_datetime(tasks['Created At'],  errors='coerce')
    tasks['resolved_ts'] = pd.to_datetime(tasks['Resolved At'], errors='coerce')
    tasks['failed_ts']   = pd.to_datetime(tasks['Failed At'],   errors='coerce')
    tasks['closed_ts']   = pd.to_datetime(tasks['Closed At'],   errors='coerce')
    tasks['created_date'] = tasks['created_ts'].dt.date

    # Last task end time = resolved / failed / closed whichever exists
    tasks['end_ts'] = tasks['resolved_ts'].fillna(tasks['failed_ts']).fillna(tasks['closed_ts'])

    return logs, tasks

logs_raw, tasks_raw = load_data()

# ── SIDEBAR FILTERS ────────────────────────────────────────────────────────────
st.sidebar.image("https://img.icons8.com/fluency/96/000000/dashboard.png", width=60)
st.sidebar.title("⚡ Ops Dashboard")
st.sidebar.markdown("---")

all_areas = sorted(tasks_raw['Area'].dropna().unique().tolist())
selected_area = st.sidebar.selectbox("📍 Area", ["All Areas"] + all_areas)

all_dates = sorted(logs_raw['date'].dropna().unique())
selected_date = st.sidebar.selectbox(
    "📅 Day",
    ["All Days"] + [str(d) for d in all_dates]
)

st.sidebar.markdown("---")
st.sidebar.markdown("<small style='color:#8b949e'>Data refreshes on reload</small>", unsafe_allow_html=True)

# ── FILTER DATA ───────────────────────────────────────────────────────────────
if selected_area == "All Areas":
    tasks = tasks_raw.copy()
    agent_names = tasks_raw['User Name'].unique()
    logs = logs_raw.copy()
else:
    tasks = tasks_raw[tasks_raw['Area'] == selected_area].copy()
    agent_names = tasks['User Name'].unique()
    logs = logs_raw[logs_raw['User Name'].isin(agent_names)].copy()

if selected_date != "All Days":
    import datetime
    sel_date = datetime.date.fromisoformat(selected_date)
    tasks = tasks[tasks['created_date'] == sel_date].copy()
    logs  = logs[logs['date'] == sel_date].copy()

# ── BUILD TIMING METRICS ──────────────────────────────────────────────────────
checkin_df  = logs[logs['Action'] == 'OPS_USER_CHECKIN'].groupby(['User Name','date'])['ts'].min().reset_index(name='checkin_time')
checkout_df = logs[logs['Action'] == 'OPS_USER_CHECKOUT'].groupby(['User Name','date'])['ts'].max().reset_index(name='checkout_time')

first_task_df = tasks.groupby(['User Name','created_date'])['created_ts'].min().reset_index()
first_task_df.columns = ['User Name','date','first_task_ts']

last_task_df = tasks.groupby(['User Name','created_date'])['end_ts'].max().reset_index()
last_task_df.columns = ['User Name','date','last_task_ts']

task_counts_df = tasks.groupby(['User Name','created_date','Status']).size().reset_index(name='count')
task_counts_pivot = task_counts_df.pivot_table(
    index=['User Name','created_date'], columns='Status', values='count', fill_value=0
).reset_index().rename(columns={'created_date':'date'})
for col in ['Success','Failed','closed']:
    if col not in task_counts_pivot.columns:
        task_counts_pivot[col] = 0
task_counts_pivot['Total'] = task_counts_pivot['Success'] + task_counts_pivot['Failed'] + task_counts_pivot['closed']
task_counts_pivot['Success %'] = (task_counts_pivot['Success'] / task_counts_pivot['Total'].replace(0, float('nan'))).fillna(0).mul(100).round(1)

# Battery swaps
swaps_df = logs[logs['Action'] == 'BATTERY_SWAP_VEHICLE'].groupby(['User Name','date']).size().reset_index(name='Swaps')

# Merge all
agent_area_map = tasks_raw.groupby('User Name')['Area'].agg(lambda x: x.mode()[0] if len(x) > 0 else '').reset_index()

daily = checkin_df.merge(checkout_df, on=['User Name','date'], how='outer')
daily = daily.merge(first_task_df, on=['User Name','date'], how='outer')
daily = daily.merge(last_task_df,  on=['User Name','date'], how='outer')
daily = daily.merge(task_counts_pivot, on=['User Name','date'], how='outer')
daily = daily.merge(swaps_df, on=['User Name','date'], how='outer')
daily = daily.merge(agent_area_map, on='User Name', how='left')

# Compute timing gaps (minutes)
daily['checkin_to_first_task_min'] = (
    (daily['first_task_ts'] - daily['checkin_time']).dt.total_seconds() / 60
).round(1)
daily['last_task_to_checkout_min'] = (
    (daily['checkout_time'] - daily['last_task_ts']).dt.total_seconds() / 60
).round(1)
daily['shift_hours'] = (
    (daily['checkout_time'] - daily['checkin_time']).dt.total_seconds() / 3600
).round(1)

# Fill numeric NaN
num_cols = daily.select_dtypes(include='number').columns
daily[num_cols] = daily[num_cols].fillna(0)

# ── KPI CARDS ─────────────────────────────────────────────────────────────────
st.markdown(f"## ⚡ Ops Performance — {selected_area} {'| ' + selected_date if selected_date != 'All Days' else ''}")
st.markdown("---")

total_agents   = daily['User Name'].nunique()
total_success  = int(tasks['Status'].eq('Success').sum())
total_failed   = int(tasks['Status'].eq('Failed').sum())
total_closed   = int(tasks['Status'].eq('closed').sum())
total_tasks    = total_success + total_failed + total_closed
success_rate   = round(total_success / total_tasks * 100, 1) if total_tasks > 0 else 0
total_swaps    = int(daily['Swaps'].sum())
avg_gap_in     = round(daily[daily['checkin_to_first_task_min'] > 0]['checkin_to_first_task_min'].mean(), 1)
avg_gap_out    = round(daily[daily['last_task_to_checkout_min'] > 0]['last_task_to_checkout_min'].mean(), 1)

c1, c2, c3, c4, c5, c6, c7, c8 = st.columns(8)
c1.metric("👥 Agents",        total_agents)
c2.metric("✅ Success",       total_success)
c3.metric("❌ Failed",        total_failed)
c4.metric("🔒 Closed",        total_closed)
c5.metric("📈 Success %",     f"{success_rate}%")
c6.metric("🔋 Swaps",         total_swaps)
c7.metric("⏱ Checkin→Task",  f"{avg_gap_in}m")
c8.metric("⏱ Task→Checkout", f"{avg_gap_out}m")

st.markdown("---")

# ── DAILY AGENT TABLE ─────────────────────────────────────────────────────────
st.markdown('<div class="section-title">📋 Daily Agent Breakdown</div>', unsafe_allow_html=True)

display_cols = {
    'User Name':                    'Agent',
    'Area':                         'Area',
    'date':                         'Date',
    'checkin_time':                 'Check In',
    'checkout_time':                'Check Out',
    'shift_hours':                  'Shift Hrs',
    'checkin_to_first_task_min':    '⏱ Checkin→1st Task (min)',
    'last_task_to_checkout_min':    '⏱ Last Task→Checkout (min)',
    'Success':                      '✅ Success',
    'Failed':                       '❌ Failed',
    'closed':                       '🔒 Closed',
    'Total':                        'Total Tasks',
    'Success %':                    'Success %',
    'Swaps':                        '🔋 Swaps',
}

table = daily[[c for c in display_cols if c in daily.columns]].rename(columns=display_cols)

# Format timestamps
for col in ['Check In','Check Out']:
    if col in table.columns:
        table[col] = pd.to_datetime(table[col]).dt.strftime('%H:%M')

table = table.sort_values(['Date','Area','Agent'] if 'Date' in table.columns else 'Agent').reset_index(drop=True)

def color_success(val):
    if isinstance(val, (int, float)):
        if val >= 75: return 'color: #3fb950; font-weight: bold'
        if val >= 50: return 'color: #e3b341; font-weight: bold'
        return 'color: #f85149; font-weight: bold'
    return ''

st.dataframe(
    table.style.applymap(color_success, subset=['Success %'] if 'Success %' in table.columns else []),
    use_container_width=True,
    height=420
)

# ── CHARTS ROW 1 ──────────────────────────────────────────────────────────────
st.markdown('<div class="section-title">📊 Task Outcomes by Agent</div>', unsafe_allow_html=True)

agent_totals = daily[daily['Total'] > 0].sort_values('Total', ascending=False).head(20)

fig_bar = go.Figure()
fig_bar.add_trace(go.Bar(name='✅ Success', x=agent_totals['User Name'].apply(lambda n: ' '.join(n.split()[:2])),
                          y=agent_totals['Success'], marker_color='#3fb950'))
fig_bar.add_trace(go.Bar(name='❌ Failed',  x=agent_totals['User Name'].apply(lambda n: ' '.join(n.split()[:2])),
                          y=agent_totals['Failed'],  marker_color='#f85149'))
fig_bar.add_trace(go.Bar(name='🔒 Closed',  x=agent_totals['User Name'].apply(lambda n: ' '.join(n.split()[:2])),
                          y=agent_totals['closed'],  marker_color='#8b949e'))
fig_bar.update_layout(
    barmode='stack', height=380,
    paper_bgcolor='#0f1117', plot_bgcolor='#1a1d2e',
    font_color='#e6edf3', legend=dict(bgcolor='#1a1d2e'),
    xaxis=dict(tickangle=-35, gridcolor='#21262d'),
    yaxis=dict(gridcolor='#21262d'),
    margin=dict(t=20, b=10)
)
st.plotly_chart(fig_bar, use_container_width=True)

# ── CHARTS ROW 2 ──────────────────────────────────────────────────────────────
col_left, col_right = st.columns(2)

with col_left:
    st.markdown('<div class="section-title">⏱ Checkin → First Task Gap (min)</div>', unsafe_allow_html=True)
    gap_in = daily[daily['checkin_to_first_task_min'] > 0].sort_values('checkin_to_first_task_min', ascending=False).head(20)
    fig_gap_in = px.bar(
        gap_in,
        x='checkin_to_first_task_min',
        y=gap_in['User Name'].apply(lambda n: ' '.join(n.split()[:2])),
        orientation='h',
        color='checkin_to_first_task_min',
        color_continuous_scale=['#3fb950','#e3b341','#f85149'],
        labels={'checkin_to_first_task_min': 'Minutes', 'y': ''}
    )
    fig_gap_in.update_layout(
        height=380, paper_bgcolor='#0f1117', plot_bgcolor='#1a1d2e',
        font_color='#e6edf3', coloraxis_showscale=False,
        xaxis=dict(gridcolor='#21262d'), yaxis=dict(gridcolor='#21262d'),
        margin=dict(t=10, b=10)
    )
    st.plotly_chart(fig_gap_in, use_container_width=True)

with col_right:
    st.markdown('<div class="section-title">⏱ Last Task → Checkout Gap (min)</div>', unsafe_allow_html=True)
    gap_out = daily[daily['last_task_to_checkout_min'] > 0].sort_values('last_task_to_checkout_min', ascending=False).head(20)
    fig_gap_out = px.bar(
        gap_out,
        x='last_task_to_checkout_min',
        y=gap_out['User Name'].apply(lambda n: ' '.join(n.split()[:2])),
        orientation='h',
        color='last_task_to_checkout_min',
        color_continuous_scale=['#3fb950','#e3b341','#f85149'],
        labels={'last_task_to_checkout_min': 'Minutes', 'y': ''}
    )
    fig_gap_out.update_layout(
        height=380, paper_bgcolor='#0f1117', plot_bgcolor='#1a1d2e',
        font_color='#e6edf3', coloraxis_showscale=False,
        xaxis=dict(gridcolor='#21262d'), yaxis=dict(gridcolor='#21262d'),
        margin=dict(t=10, b=10)
    )
    st.plotly_chart(fig_gap_out, use_container_width=True)

# ── CHARTS ROW 3 ──────────────────────────────────────────────────────────────
col3, col4 = st.columns(2)

with col3:
    st.markdown('<div class="section-title">🔋 Battery Swaps per Agent</div>', unsafe_allow_html=True)
    swap_data = daily[daily['Swaps'] > 0].sort_values('Swaps', ascending=False).head(20)
    fig_swaps = px.bar(
        swap_data,
        x=swap_data['User Name'].apply(lambda n: ' '.join(n.split()[:2])),
        y='Swaps',
        color='Swaps',
        color_continuous_scale=['#1a1d2e','#39d353'],
        labels={'x': '', 'Swaps': 'Swaps'}
    )
    fig_swaps.update_layout(
        height=350, paper_bgcolor='#0f1117', plot_bgcolor='#1a1d2e',
        font_color='#e6edf3', coloraxis_showscale=False,
        xaxis=dict(tickangle=-35, gridcolor='#21262d'),
        yaxis=dict(gridcolor='#21262d'),
        margin=dict(t=10, b=10)
    )
    st.plotly_chart(fig_swaps, use_container_width=True)

with col4:
    st.markdown('<div class="section-title">📍 Task Outcomes by Area</div>', unsafe_allow_html=True)
    area_data = tasks_raw.copy()
    if selected_date != 'All Days':
        import datetime
        area_data = area_data[area_data['created_date'] == datetime.date.fromisoformat(selected_date)]
    area_counts = area_data.groupby(['Area','Status']).size().reset_index(name='count')
    fig_area = px.bar(
        area_counts, x='Area', y='count', color='Status',
        color_discrete_map={'Success':'#3fb950','Failed':'#f85149','closed':'#8b949e'},
        barmode='stack', labels={'count': 'Tasks'}
    )
    fig_area.update_layout(
        height=350, paper_bgcolor='#0f1117', plot_bgcolor='#1a1d2e',
        font_color='#e6edf3', legend=dict(bgcolor='#1a1d2e'),
        xaxis=dict(gridcolor='#21262d'), yaxis=dict(gridcolor='#21262d'),
        margin=dict(t=10, b=10)
    )
    st.plotly_chart(fig_area, use_container_width=True)

# ── SHIFT HOURS ───────────────────────────────────────────────────────────────
st.markdown('<div class="section-title">🕐 Shift Hours per Agent</div>', unsafe_allow_html=True)
shift_data = daily[daily['shift_hours'] > 0].sort_values('shift_hours', ascending=False)
shift_data['color'] = shift_data['shift_hours'].apply(
    lambda h: '#3fb950' if h >= 8 else ('#e3b341' if h >= 4 else '#f85149')
)
fig_shift = px.bar(
    shift_data,
    x=shift_data['User Name'].apply(lambda n: ' '.join(n.split()[:2])),
    y='shift_hours',
    color='color', color_discrete_map='identity',
    labels={'x': '', 'shift_hours': 'Hours'}
)
fig_shift.add_hline(y=8, line_dash='dash', line_color='#e3b341', opacity=0.5, annotation_text='8h')
fig_shift.update_layout(
    height=320, paper_bgcolor='#0f1117', plot_bgcolor='#1a1d2e',
    font_color='#e6edf3', showlegend=False,
    xaxis=dict(tickangle=-35, gridcolor='#21262d'),
    yaxis=dict(gridcolor='#21262d'),
    margin=dict(t=10, b=10)
)
st.plotly_chart(fig_shift, use_container_width=True)
