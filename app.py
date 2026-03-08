import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import datetime
import warnings
warnings.filterwarnings('ignore')

st.set_page_config(page_title="Ops Team Dashboard", page_icon="⚡", layout="wide")

st.markdown("""
<style>
    .main { background-color: #0f1117; }
    .block-container { padding-top: 1.5rem; padding-bottom: 1rem; }
    .section-title {
        font-size: 1.05rem; font-weight: 700; color: #e6edf3;
        margin: 1.4rem 0 0.5rem 0;
        border-left: 3px solid #58a6ff; padding-left: 10px;
    }
    div[data-testid="stMetric"] {
        background: #1a1d2e; border-radius: 10px;
        padding: 12px 18px; border: 1px solid #2d3250;
    }
    div[data-testid="stMetric"] label { color: #8b949e !important; font-size: 0.78rem !important; }
    div[data-testid="stMetricValue"] { color: #e6edf3 !important; font-size: 1.6rem !important; font-weight: 800 !important; }
</style>
""", unsafe_allow_html=True)

# SIDEBAR
st.sidebar.title("Ops Dashboard")
st.sidebar.markdown("---")
st.sidebar.markdown("### Upload Data")
uploaded_logs  = st.sidebar.file_uploader("User Logs (.xlsx)",  type=["xlsx"], key="logs")
uploaded_tasks = st.sidebar.file_uploader("Ops Tasks (.xlsx)", type=["xlsx"], key="tasks")
st.sidebar.markdown("---")

if uploaded_logs is None or uploaded_tasks is None:
    st.markdown("## Ops Team Dashboard")
    st.info("Upload both User Logs.xlsx and Ops Tasks.xlsx from the sidebar to load the dashboard.")
    st.stop()

@st.cache_data
def load_and_build(logs_file, tasks_file):
    logs  = pd.read_excel(logs_file)
    tasks = pd.read_excel(tasks_file)

    logs['User Name']  = logs['User Name'].str.strip()
    tasks['User Name'] = tasks['User Name'].str.strip()

    logs['ts'] = pd.to_datetime(logs['Date (Local)'], errors='coerce')

    tasks['created_ts']  = pd.to_datetime(tasks['Created At'],  errors='coerce')
    tasks['resolved_ts'] = pd.to_datetime(tasks['Resolved At'], errors='coerce')
    tasks['failed_ts']   = pd.to_datetime(tasks['Failed At'],   errors='coerce')
    tasks['closed_ts']   = pd.to_datetime(tasks['Closed At'],   errors='coerce')
    tasks['end_ts'] = tasks['resolved_ts'].fillna(tasks['failed_ts']).fillna(tasks['closed_ts'])

    ci = logs[logs['Action'] == 'OPS_USER_CHECKIN'][['User Name','ts']].sort_values(['User Name','ts'])
    co = logs[logs['Action'] == 'OPS_USER_CHECKOUT'][['User Name','ts']].sort_values(['User Name','ts'])

    sessions = []
    for agent in ci['User Name'].unique():
        a_ci = ci[ci['User Name'] == agent]['ts'].tolist()
        a_co = co[co['User Name'] == agent]['ts'].tolist()
        for cin in a_ci:
            after = [c for c in a_co if c > cin]
            cout  = after[0] if after else None
            sessions.append({'User Name': agent, 'checkin': cin, 'checkout': cout, 'shift_date': cin.date()})

    sessions_df = pd.DataFrame(sessions)
    sessions_df['shift_hours'] = ((sessions_df['checkout'] - sessions_df['checkin']).dt.total_seconds() / 3600).round(1)

    session_lookup = {}
    for _, s in sessions_df.iterrows():
        session_lookup.setdefault(s['User Name'], []).append((s['checkin'], s['checkout'], s['shift_date']))

    def find_shift(agent, t):
        if pd.isna(t) or agent not in session_lookup:
            return None
        for cin, cout, sd in session_lookup[agent]:
            if pd.isna(cout):
                if t >= cin: return sd
            elif cin <= t <= cout:
                return sd
        return None

    tasks['shift_date'] = tasks.apply(lambda r: find_shift(r['User Name'], r['created_ts']), axis=1)

    swap_logs = logs[logs['Action'] == 'BATTERY_SWAP_VEHICLE'][['User Name','ts']].copy()
    swap_logs['shift_date'] = swap_logs.apply(lambda r: find_shift(r['User Name'], r['ts']), axis=1)
    swaps_df = swap_logs.dropna(subset=['shift_date']).groupby(['User Name','shift_date']).size().reset_index(name='Swaps')

    tasks_s    = tasks.dropna(subset=['shift_date'])
    # Combine ALL log actions (excl checkin/checkout) + ALL task timestamps
    # to get the true first and last action per session
    task_ts_all = pd.concat([
        tasks_s[['User Name','shift_date','created_ts']].rename(columns={'created_ts':'ts'}),
        tasks_s[['User Name','shift_date','resolved_ts']].rename(columns={'resolved_ts':'ts'}),
        tasks_s[['User Name','shift_date','failed_ts']].rename(columns={'failed_ts':'ts'}),
        tasks_s[['User Name','shift_date','closed_ts']].rename(columns={'closed_ts':'ts'}),
    ]).dropna(subset=['ts'])

    # Assign shift_date to log actions too
    log_action_ts = logs[~logs['Action'].isin(['OPS_USER_CHECKIN','OPS_USER_CHECKOUT'])][['User Name','ts']].copy()
    log_action_ts['shift_date'] = log_action_ts.apply(lambda r: find_shift(r['User Name'], r['ts']), axis=1)
    log_action_ts = log_action_ts.dropna(subset=['shift_date'])

    all_actions_df = pd.concat([
        task_ts_all[['User Name','shift_date','ts']],
        log_action_ts[['User Name','shift_date','ts']]
    ]).dropna(subset=['ts'])

    first_task = all_actions_df.groupby(['User Name','shift_date'])['ts'].min().reset_index(name='first_task_ts')
    last_task  = all_actions_df.groupby(['User Name','shift_date'])['ts'].max().reset_index(name='last_task_ts')

    task_counts = (
        tasks_s.groupby(['User Name','shift_date','Status']).size()
        .reset_index(name='count')
        .pivot_table(index=['User Name','shift_date'], columns='Status', values='count', fill_value=0)
        .reset_index()
    )
    for col in ['Success','Failed','closed']:
        if col not in task_counts.columns: task_counts[col] = 0
    task_counts['Total']     = task_counts['Success'] + task_counts['Failed'] + task_counts['closed']
    task_counts['Success %'] = (task_counts['Success'] / task_counts['Total'].replace(0, float('nan'))).fillna(0).mul(100).round(0).astype(int)

    area_map = tasks.groupby('User Name')['Area'].agg(lambda x: x.mode()[0] if len(x) > 0 else '').reset_index()

    daily = sessions_df.merge(task_counts, on=['User Name','shift_date'], how='inner')
    daily = daily.merge(first_task, on=['User Name','shift_date'], how='left')
    daily = daily.merge(last_task,  on=['User Name','shift_date'], how='left')
    daily = daily.merge(swaps_df,   on=['User Name','shift_date'], how='left')
    daily = daily.merge(area_map,   on='User Name',                how='left')

    daily['checkin_to_first_task_min'] = ((daily['first_task_ts'] - daily['checkin']).dt.total_seconds() / 60).round(0).astype('Int64')
    daily['last_task_to_checkout_min'] = ((daily['checkout'] - daily['last_task_ts']).dt.total_seconds() / 60).round(0).astype('Int64')

    for col in ['Success','Failed','closed','Total','Swaps']:
        if col in daily.columns:
            daily[col] = daily[col].fillna(0).astype(int)

    daily = daily[daily['Total'] > 0].copy()
    return daily, tasks, area_map

daily_all, tasks_raw, area_map = load_and_build(uploaded_logs, uploaded_tasks)

def fmt_shift_date(d):
    if isinstance(d, str):
        d = datetime.date.fromisoformat(d)
    return d.strftime('%d %B %A')

shift_dates_raw    = sorted(daily_all['shift_date'].dropna().unique())
shift_date_options = ["All Shifts"] + [fmt_shift_date(d) for d in shift_dates_raw]
shift_date_map     = {fmt_shift_date(d): d for d in shift_dates_raw}

all_areas = sorted(daily_all['Area'].dropna().unique().tolist())
sel_area  = st.sidebar.selectbox("Area",      ["All Areas"] + all_areas)
sel_date  = st.sidebar.selectbox("Shift Day", shift_date_options)
st.sidebar.markdown("---")
st.sidebar.caption("Shift = checkin to checkout. Night shifts grouped under checkin day.")

daily = daily_all.copy()
if sel_area != "All Areas":
    daily = daily[daily['Area'] == sel_area]
if sel_date != "All Shifts":
    sd    = shift_date_map[sel_date]
    daily = daily[daily['shift_date'] == sd]

# KPI CARDS
st.markdown(f"## Ops Performance  {sel_area}  |  {sel_date}")
st.markdown("---")

total_agents  = daily['User Name'].nunique()
total_success = int(daily['Success'].sum())
total_failed  = int(daily['Failed'].sum())
total_closed  = int(daily['closed'].sum())
total_tasks   = total_success + total_failed + total_closed
success_rate  = int(round(total_success / total_tasks * 100)) if total_tasks > 0 else 0
total_swaps   = int(daily['Swaps'].sum())

c1,c2,c3,c4,c5,c6 = st.columns(6)
c1.metric("Agents",    total_agents)
c2.metric("Success",   total_success)
c3.metric("Failed",    total_failed)
c4.metric("Closed",    total_closed)
c5.metric("Success %", f"{success_rate}%")
c6.metric("Swaps",     total_swaps)
st.markdown("---")

# AGENT TABLE
st.markdown('<div class="section-title">Daily Agent Breakdown</div>', unsafe_allow_html=True)

table = daily[['User Name','Area','shift_date','checkin','checkout','shift_hours',
               'checkin_to_first_task_min','last_task_to_checkout_min',
               'Success','Failed','closed','Total','Success %','Swaps']].copy()

table['shift_date'] = pd.to_datetime(table['shift_date']).dt.strftime('%d %B %A')
table['checkin']    = pd.to_datetime(table['checkin'],  errors='coerce').dt.strftime('%d %b %H:%M').fillna('-')
table['checkout']   = pd.to_datetime(table['checkout'], errors='coerce').dt.strftime('%d %b %H:%M').fillna('-')
table['shift_hours'] = table['shift_hours'].apply(
    lambda x: int(x) if pd.notna(x) and float(x) == int(float(x)) else round(float(x), 1) if pd.notna(x) else '-'
)

table.columns = ['Agent','Area','Shift Day','Check In','Check Out','Shift Hrs',
                 'Checkin to 1st Action (min)','Last Action to Checkout (min)',
                 'Success','Failed','Closed','Total','Success %','Swaps']
table = table.sort_values(['Shift Day','Area','Agent']).reset_index(drop=True)

def color_success(val):
    if isinstance(val, (int, float)):
        if val >= 75: return 'color: #3fb950; font-weight: bold'
        if val >= 50: return 'color: #e3b341; font-weight: bold'
        if val > 0:   return 'color: #f85149; font-weight: bold'
    return ''

st.dataframe(table.style.applymap(color_success, subset=['Success %']), use_container_width=True, height=430)

# TASK OUTCOMES BY AGENT
st.markdown('<div class="section-title">Task Outcomes by Agent</div>', unsafe_allow_html=True)
agg    = daily.groupby('User Name')[['Success','Failed','closed','Total']].sum().reset_index()
agg    = agg[agg['Total'] > 0].sort_values('Total', ascending=False).head(25)
labels = agg['User Name'].apply(lambda n: ' '.join(str(n).split()[:2]))

fig_bar = go.Figure()
fig_bar.add_trace(go.Bar(name='Success', x=labels, y=agg['Success'], marker_color='#3fb950'))
fig_bar.add_trace(go.Bar(name='Failed',  x=labels, y=agg['Failed'],  marker_color='#f85149'))
fig_bar.add_trace(go.Bar(name='Closed',  x=labels, y=agg['closed'],  marker_color='#8b949e'))
fig_bar.update_layout(barmode='stack', height=400, paper_bgcolor='#0f1117', plot_bgcolor='#1a1d2e',
    font_color='#e6edf3', legend=dict(bgcolor='#1a1d2e'),
    xaxis=dict(tickangle=-35, gridcolor='#21262d'), yaxis=dict(gridcolor='#21262d', tickformat='d'),
    margin=dict(t=20, b=10))
st.plotly_chart(fig_bar, use_container_width=True)

# GAP CHARTS
col_left, col_right = st.columns(2)

with col_left:
    st.markdown('<div class="section-title">Checkin to First Action (avg min)</div>', unsafe_allow_html=True)
    gap_in = (daily[daily['checkin_to_first_task_min'] > 0]
        .groupby('User Name')['checkin_to_first_task_min'].mean().round(0).astype(int).reset_index()
        .sort_values('checkin_to_first_task_min', ascending=True))
    if len(gap_in) > 0:
        fig_gin = px.bar(gap_in, x='checkin_to_first_task_min',
            y=gap_in['User Name'].apply(lambda n: ' '.join(str(n).split()[:2])),
            orientation='h', color='checkin_to_first_task_min',
            color_continuous_scale=['#3fb950','#e3b341','#f85149'],
            text='checkin_to_first_task_min', labels={'checkin_to_first_task_min':'Minutes','y':''})
        fig_gin.update_traces(textposition='outside')
        fig_gin.update_layout(height=430, paper_bgcolor='#0f1117', plot_bgcolor='#1a1d2e',
            font_color='#e6edf3', coloraxis_showscale=False,
            xaxis=dict(gridcolor='#21262d', tickformat='d'), yaxis=dict(gridcolor='#21262d'),
            margin=dict(t=10, b=10))
        st.plotly_chart(fig_gin, use_container_width=True)

with col_right:
    st.markdown('<div class="section-title">Last Action to Checkout (avg min)</div>', unsafe_allow_html=True)
    gap_out = (daily[daily['last_task_to_checkout_min'] > 0]
        .groupby('User Name')['last_task_to_checkout_min'].mean().round(0).astype(int).reset_index()
        .sort_values('last_task_to_checkout_min', ascending=True))
    if len(gap_out) > 0:
        fig_gout = px.bar(gap_out, x='last_task_to_checkout_min',
            y=gap_out['User Name'].apply(lambda n: ' '.join(str(n).split()[:2])),
            orientation='h', color='last_task_to_checkout_min',
            color_continuous_scale=['#3fb950','#e3b341','#f85149'],
            text='last_task_to_checkout_min', labels={'last_task_to_checkout_min':'Minutes','y':''})
        fig_gout.update_traces(textposition='outside')
        fig_gout.update_layout(height=430, paper_bgcolor='#0f1117', plot_bgcolor='#1a1d2e',
            font_color='#e6edf3', coloraxis_showscale=False,
            xaxis=dict(gridcolor='#21262d', tickformat='d'), yaxis=dict(gridcolor='#21262d'),
            margin=dict(t=10, b=10))
        st.plotly_chart(fig_gout, use_container_width=True)

# SWAPS & AREA
col3, col4 = st.columns(2)

with col3:
    st.markdown('<div class="section-title">Battery Swaps per Agent</div>', unsafe_allow_html=True)
    swap_agg = daily.groupby('User Name')['Swaps'].sum().reset_index()
    swap_agg = swap_agg[swap_agg['Swaps'] > 0].sort_values('Swaps', ascending=False).head(20)
    if len(swap_agg) > 0:
        fig_swaps = px.bar(swap_agg,
            x=swap_agg['User Name'].apply(lambda n: ' '.join(str(n).split()[:2])),
            y='Swaps', color='Swaps', color_continuous_scale=['#1a1d2e','#39d353'],
            text='Swaps', labels={'x':'','Swaps':'Swaps'})
        fig_swaps.update_traces(textposition='outside')
        fig_swaps.update_layout(height=380, paper_bgcolor='#0f1117', plot_bgcolor='#1a1d2e',
            font_color='#e6edf3', coloraxis_showscale=False,
            xaxis=dict(tickangle=-35, gridcolor='#21262d'), yaxis=dict(gridcolor='#21262d', tickformat='d'),
            margin=dict(t=10, b=10))
        st.plotly_chart(fig_swaps, use_container_width=True)

with col4:
    st.markdown('<div class="section-title">Task Outcomes by Area</div>', unsafe_allow_html=True)
    area_agg = daily.groupby('Area')[['Success','Failed','closed']].sum().reset_index()
    area_agg = area_agg[area_agg['Area'].notna() & (area_agg['Area'] != '')]
    area_melted = area_agg.melt(id_vars='Area', value_vars=['Success','Failed','closed'], var_name='Status', value_name='count')
    area_melted = area_melted[area_melted['count'] > 0]
    if len(area_melted) > 0:
        fig_area = px.bar(area_melted, x='Area', y='count', color='Status',
            color_discrete_map={'Success':'#3fb950','Failed':'#f85149','closed':'#8b949e'},
            barmode='stack', labels={'count':'Tasks'})
        fig_area.update_layout(height=380, paper_bgcolor='#0f1117', plot_bgcolor='#1a1d2e',
            font_color='#e6edf3', legend=dict(bgcolor='#1a1d2e'),
            xaxis=dict(gridcolor='#21262d'), yaxis=dict(gridcolor='#21262d', tickformat='d'),
            margin=dict(t=10, b=10))
        st.plotly_chart(fig_area, use_container_width=True)

# SHIFT HOURS
st.markdown('<div class="section-title">Shift Hours per Agent</div>', unsafe_allow_html=True)
shift_agg = daily.groupby('User Name')['shift_hours'].sum().reset_index()
shift_agg = shift_agg[shift_agg['shift_hours'] > 0].sort_values('shift_hours', ascending=False)
if len(shift_agg) > 0:
    shift_agg['color'] = shift_agg['shift_hours'].apply(lambda h: '#3fb950' if h >= 8 else ('#e3b341' if h >= 4 else '#f85149'))
    shift_agg['label'] = shift_agg['shift_hours'].apply(lambda h: str(int(h)) if float(h) == int(float(h)) else str(round(h, 1)))
    fig_shift = px.bar(shift_agg,
        x=shift_agg['User Name'].apply(lambda n: ' '.join(str(n).split()[:2])),
        y='shift_hours', color='color', color_discrete_map='identity',
        text='label', labels={'x':'','shift_hours':'Hours'})
    fig_shift.update_traces(textposition='outside')
    fig_shift.add_hline(y=8, line_dash='dash', line_color='#e3b341', opacity=0.6, annotation_text='8h target')
    fig_shift.update_layout(height=340, paper_bgcolor='#0f1117', plot_bgcolor='#1a1d2e',
        font_color='#e6edf3', showlegend=False,
        xaxis=dict(tickangle=-35, gridcolor='#21262d'), yaxis=dict(gridcolor='#21262d', tickformat='d'),
        margin=dict(t=10, b=10))
    st.plotly_chart(fig_shift, use_container_width=True)
