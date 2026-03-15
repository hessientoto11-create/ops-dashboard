import streamlit as st
import io
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
import os, time

st.sidebar.title("Ops Dashboard")
st.sidebar.markdown("---")
st.sidebar.markdown("### 📂 Upload Data")
uploaded_logs  = st.sidebar.file_uploader("User Logs (.xlsx)",  type=["xlsx"], key="logs")
uploaded_tasks = st.sidebar.file_uploader("Ops Tasks (.xlsx)", type=["xlsx"], key="tasks")
st.sidebar.markdown("---")

# ── PERSISTENT STORAGE: save files to disk on the server ─────────────────────
# Files are stored on the Streamlit Cloud server filesystem and survive
# browser close/reopen as long as the server instance is running.
STORAGE_DIR   = ".streamlit_data"
LOGS_SAVED    = os.path.join(STORAGE_DIR, "user_logs.xlsx")
TASKS_SAVED   = os.path.join(STORAGE_DIR, "ops_tasks.xlsx")
META_LOGS     = os.path.join(STORAGE_DIR, "logs_name.txt")
META_TASKS    = os.path.join(STORAGE_DIR, "tasks_name.txt")
os.makedirs(STORAGE_DIR, exist_ok=True)

if uploaded_logs is not None:
    raw = uploaded_logs.read()
    with open(LOGS_SAVED, 'wb') as f: f.write(raw)
    with open(META_LOGS,  'w') as f:  f.write(uploaded_logs.name)
    st.session_state['logs_bytes'] = raw
    st.session_state['logs_name']  = uploaded_logs.name
    st.cache_data.clear()

if uploaded_tasks is not None:
    raw = uploaded_tasks.read()
    with open(TASKS_SAVED, 'wb') as f: f.write(raw)
    with open(META_TASKS,  'w') as f:  f.write(uploaded_tasks.name)
    st.session_state['tasks_bytes'] = raw
    st.session_state['tasks_name']  = uploaded_tasks.name
    st.cache_data.clear()

# Restore from disk if session was lost (browser closed and reopened)
if 'logs_bytes' not in st.session_state and os.path.exists(LOGS_SAVED):
    with open(LOGS_SAVED, 'rb') as f:
        st.session_state['logs_bytes'] = f.read()
    with open(META_LOGS, 'r') as f:
        st.session_state['logs_name'] = f.read().strip()

if 'tasks_bytes' not in st.session_state and os.path.exists(TASKS_SAVED):
    with open(TASKS_SAVED, 'rb') as f:
        st.session_state['tasks_bytes'] = f.read()
    with open(META_TASKS, 'r') as f:
        st.session_state['tasks_name'] = f.read().strip()

logs_ready  = 'logs_bytes'  in st.session_state
tasks_ready = 'tasks_bytes' in st.session_state

if logs_ready:
    mtime = os.path.getmtime(LOGS_SAVED) if os.path.exists(LOGS_SAVED) else None
    mtime_str = time.strftime(' (%d %b %H:%M)', time.localtime(mtime)) if mtime else ''
    st.sidebar.success(f"✅ {st.session_state['logs_name']}{mtime_str}")
if tasks_ready:
    mtime = os.path.getmtime(TASKS_SAVED) if os.path.exists(TASKS_SAVED) else None
    mtime_str = time.strftime(' (%d %b %H:%M)', time.localtime(mtime)) if mtime else ''
    st.sidebar.success(f"✅ {st.session_state['tasks_name']}{mtime_str}")

if not logs_ready or not tasks_ready:
    st.markdown("## Ops Team Dashboard")
    st.info("👈 Upload both **User Logs.xlsx** and **Ops Tasks.xlsx** from the sidebar to load the dashboard.")
    st.stop()


@st.cache_data
def load_and_build(logs_bytes, tasks_bytes):
    import io
    logs  = pd.read_excel(io.BytesIO(logs_bytes))
    tasks = pd.read_excel(io.BytesIO(tasks_bytes))

    # Validate required columns
    required_logs  = {'User Name','Action','Vehicle','Date (Local)'}
    required_tasks = {'User Name','Status','Area','Created At'}
    missing_logs   = required_logs  - set(logs.columns)
    missing_tasks  = required_tasks - set(tasks.columns)
    if missing_logs:  raise ValueError(f"User Logs missing columns: {missing_logs}")
    if missing_tasks: raise ValueError(f"Ops Tasks missing columns: {missing_tasks}")

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

    # shift_date: checkins 00:00-05:59 belong to the previous calendar day
    # (night-shift agents cross midnight — their shift started the evening before)
    def shift_date_for(ts):
        return (ts - pd.Timedelta(days=1)).date() if ts.hour < 6 else ts.date()

    # Pair checkins and checkouts purely chronologically per agent:
    # - Walk all CI/CO events in time order
    # - Each CI opens a pending session
    # - The next CO closes it
    # - If another CI arrives before any CO → previous session checkout = Missed
    # - CO with no pending CI → orphan (ignored)
    sessions = []
    for agent in ci['User Name'].unique():
        a_ci = ci[ci['User Name'] == agent]['ts'].sort_values().tolist()
        a_co = co[co['User Name'] == agent]['ts'].sort_values().tolist()
        events = sorted([(t, 'CI') for t in a_ci] + [(t, 'CO') for t in a_co], key=lambda x: x[0])
        pending_cin = None
        for ts, typ in events:
            if typ == 'CI':
                if pending_cin is not None:
                    # New checkin before checkout → previous shift missed checkout
                    sessions.append({'User Name': agent, 'checkin': pending_cin, 'checkout': None,
                                     'shift_date': shift_date_for(pending_cin)})
                pending_cin = ts
            else:  # CO
                if pending_cin is not None:
                    sessions.append({'User Name': agent, 'checkin': pending_cin, 'checkout': ts,
                                     'shift_date': shift_date_for(pending_cin)})
                    pending_cin = None
                # else: orphan checkout (no matching checkin) — ignore
        if pending_cin is not None:
            sessions.append({'User Name': agent, 'checkin': pending_cin, 'checkout': None,
                             'shift_date': shift_date_for(pending_cin)})

    sessions_df = pd.DataFrame(sessions)
    sessions_df['shift_hours'] = ((sessions_df['checkout'] - sessions_df['checkin']).dt.total_seconds() / 3600).round(1)

    # Assign a unique session_id per session — used as the join key throughout
    # This avoids bugs when two sessions share the same (User Name, shift_date)
    sessions_df = sessions_df.sort_values(['User Name','checkin']).reset_index(drop=True)
    sessions_df['session_id'] = sessions_df.index.astype(str)

    # Build session lookup: agent → list of (checkin, checkout_cap, session_id)
    # Missed-checkout sessions are capped at the next checkin for that agent
    session_lookup = {}
    session_window  = {}
    for idx, s in sessions_df.iterrows():
        agent = s['User Name']; cin = s['checkin']; cout = s['checkout']
        sid   = s['session_id']
        if pd.isna(cout):
            nxt = sessions_df[(sessions_df['User Name'] == agent) & (sessions_df['checkin'] > cin)]
            cout_cap = nxt.iloc[0]['checkin'] if len(nxt) > 0 else cin + pd.Timedelta(hours=24)
        else:
            cout_cap = cout
        session_lookup.setdefault(agent, []).append((cin, cout_cap, sid))
        session_window[sid] = (cin, cout_cap)

    def find_session(agent, t):
        """Return session_id for timestamp t, or None if outside all windows."""
        if pd.isna(t) or agent not in session_lookup:
            return None
        for cin, cout_cap, sid in session_lookup[agent]:
            if cin <= t <= cout_cap:
                return sid
        return None

    tasks['session_id'] = tasks.apply(lambda r: find_session(r['User Name'], r['created_ts']), axis=1)
    # Keep shift_date for display, looked up from sessions_df
    sid_to_sd = sessions_df.set_index('session_id')['shift_date'].to_dict()
    sid_to_agent = sessions_df.set_index('session_id')['User Name'].to_dict()
    tasks['shift_date'] = tasks['session_id'].map(sid_to_sd)

    swap_logs = logs[logs['Action'] == 'BATTERY_SWAP_VEHICLE'][['User Name','ts']].copy()
    swap_logs['session_id'] = swap_logs.apply(lambda r: find_session(r['User Name'], r['ts']), axis=1)
    swaps_df = swap_logs.dropna(subset=['session_id']).groupby('session_id').size().reset_index(name='Swaps')

    # Activate / Deactivate — dedup: same agent + same action + same vehicle + same hour = count as 1
    act_dfs_list = []
    for action, col_name in [('ACTIVATED_VEHICLE','Activated'), ('DEACTIVATED_VEHICLE','Deactivated')]:
        act_logs = logs[logs['Action'] == action][['User Name','Vehicle','ts']].copy()
        act_logs['hour_bucket'] = act_logs['ts'].dt.floor('h')
        act_logs = act_logs.drop_duplicates(subset=['User Name','Vehicle','hour_bucket'])
        act_logs['session_id'] = act_logs.apply(lambda r: find_session(r['User Name'], r['ts']), axis=1)
        act_df = act_logs.dropna(subset=['session_id']).groupby('session_id').size().reset_index(name=col_name)
        act_dfs_list.append(act_df)
    act_dfs = act_dfs_list[0].merge(act_dfs_list[1], on='session_id', how='outer')

    tasks_s = tasks.dropna(subset=['session_id'])

    # Helper: only include a timestamp if it falls inside the actual session window
    def in_window(sid, t):
        if sid not in session_window or pd.isna(t): return False
        cin, cout_cap = session_window[sid]
        return cin <= t <= cout_cap

    # Task timestamps — only those within their session window
    task_ts_rows = []
    for _, row in tasks_s.iterrows():
        sid = row['session_id']
        for col in ['created_ts','resolved_ts','failed_ts','closed_ts']:
            t = row[col]
            if pd.notna(t) and in_window(sid, t):
                task_ts_rows.append({'session_id': sid, 'ts': t})
    task_ts_all = pd.DataFrame(task_ts_rows) if task_ts_rows else pd.DataFrame(columns=['session_id','ts'])

    # Log actions — assigned to session by find_shift (already window-correct)
    log_action_ts = logs[~logs['Action'].isin(['OPS_USER_CHECKIN','OPS_USER_CHECKOUT'])][['User Name','ts']].copy()
    log_action_ts['session_id'] = log_action_ts.apply(lambda r: find_session(r['User Name'], r['ts']), axis=1)
    log_action_ts = log_action_ts.dropna(subset=['session_id'])

    all_actions_df = pd.concat([
        task_ts_all[['session_id','ts']],
        log_action_ts[['session_id','ts']]
    ]).dropna(subset=['ts'])

    first_task = all_actions_df.groupby('session_id')['ts'].min().reset_index(name='first_task_ts')
    last_task  = all_actions_df.groupby('session_id')['ts'].max().reset_index(name='last_task_ts')

    # Dynamic status columns — works with any status values in the file
    task_counts = (
        tasks_s.groupby(['session_id','Status']).size()
        .reset_index(name='count')
        .pivot_table(index='session_id', columns='Status', values='count', fill_value=0)
        .reset_index()
    )
    task_counts.columns.name = None

    # Detect status columns dynamically
    fixed_cols   = ['session_id']
    status_cols  = [c for c in task_counts.columns if c not in fixed_cols]
    success_cols = [c for c in status_cols if str(c).lower() in ('success',)]
    failed_cols  = [c for c in status_cols if str(c).lower() in ('failed',)]
    closed_cols  = [c for c in status_cols if str(c).lower() in ('closed',)]
    other_cols   = [c for c in status_cols if c not in success_cols + failed_cols + closed_cols]

    task_counts['Success']  = task_counts[success_cols].sum(axis=1) if success_cols else 0
    task_counts['Failed']   = task_counts[failed_cols].sum(axis=1)  if failed_cols  else 0
    task_counts['closed']   = task_counts[closed_cols].sum(axis=1)  if closed_cols  else 0
    task_counts['Other']    = task_counts[other_cols].sum(axis=1)   if other_cols   else 0
    task_counts['Total']    = task_counts['Success'] + task_counts['Failed'] + task_counts['closed'] + task_counts['Other']
    task_counts['Success %'] = (task_counts['Success'] / task_counts['Total'].replace(0, float('nan'))).fillna(0).mul(100).round(0).astype(int)

    area_map = tasks.groupby('User Name')['Area'].agg(lambda x: x.mode()[0] if len(x) > 0 else '').reset_index()

    daily = sessions_df.merge(task_counts, on='session_id', how='inner')
    daily = daily.merge(first_task, on='session_id', how='left')
    daily = daily.merge(last_task,  on='session_id', how='left')
    daily = daily.merge(swaps_df,   on='session_id', how='left')
    daily = daily.merge(act_dfs,    on='session_id', how='left')
    daily = daily.merge(area_map,   on='User Name',  how='left')

    daily['checkin_to_first_min'] = ((daily['first_task_ts'] - daily['checkin']).dt.total_seconds() / 60).round(0).astype('Int64')
    daily['last_to_checkout_min'] = ((daily['checkout'] - daily['last_task_ts']).dt.total_seconds() / 60).round(0).astype('Int64')

    for col in ['Success','Failed','closed','Other','Total','Swaps','Activated','Deactivated']:
        if col in daily.columns:
            daily[col] = daily[col].fillna(0).astype(int)

    daily = daily[daily['Total'] > 0].copy()
    return daily, tasks, area_map

daily_all, tasks_raw, area_map = load_and_build(st.session_state['logs_bytes'], st.session_state['tasks_bytes'])

def fmt_shift_date(d):
    if isinstance(d, str):
        d = datetime.date.fromisoformat(d)
    return d.strftime('%d %B %A')

shift_dates_raw    = sorted(daily_all['shift_date'].dropna().unique())
shift_date_options = ["All Shifts"] + [fmt_shift_date(d) for d in shift_dates_raw]
shift_date_map     = {fmt_shift_date(d): d for d in shift_dates_raw}

all_areas = sorted(daily_all['Area'].dropna().unique().tolist())
sel_area  = st.sidebar.selectbox("Area",      ["All Areas"] + all_areas)
sel_date  = st.sidebar.multiselect("Shift Day", shift_date_options[1:], default=[])
st.sidebar.markdown("---")
st.sidebar.caption("Shift = checkin to checkout. Night shifts grouped under checkin day.")

daily = daily_all.copy()
if sel_area != "All Areas":
    daily = daily[daily['Area'] == sel_area]
if sel_date:
    sds   = [shift_date_map[d] for d in sel_date]
    daily = daily[daily['shift_date'].isin(sds)]

# KPI CARDS
date_label = ", ".join(sel_date) if sel_date else "All Shifts"
st.markdown(f"## Ops Performance  {sel_area}  |  {date_label}")
st.markdown("---")

total_agents  = daily['User Name'].nunique()
total_success = int(daily['Success'].sum())
total_failed  = int(daily['Failed'].sum())
total_closed  = int(daily['closed'].sum())
total_other   = int(daily['Other'].sum()) if 'Other' in daily.columns else 0
total_tasks   = total_success + total_failed + total_closed + total_other
success_rate  = int(round(total_success / total_tasks * 100)) if total_tasks > 0 else 0
total_swaps   = int(daily['Swaps'].sum())
total_activated   = int(daily['Activated'].sum()) if 'Activated' in daily.columns else 0
total_deactivated = int(daily['Deactivated'].sum()) if 'Deactivated' in daily.columns else 0

c1,c2,c3,c4,c5,c6,c7,c8,c9 = st.columns(9)
c1.metric("Agents",       total_agents)
c2.metric("Success",      total_success)
c3.metric("Failed",       total_failed)
c4.metric("Closed",       total_closed)
c5.metric("Total Tasks",  total_tasks)
c6.metric("Success %",    f"{success_rate}%")
c7.metric("Swaps",        total_swaps)
c8.metric("Activated",    total_activated)
c9.metric("Deactivated",  total_deactivated)
st.markdown("---")

# AGENT TABLE
st.markdown('<div class="section-title">Daily Agent Breakdown</div>', unsafe_allow_html=True)

# Ensure optional columns exist
for col in ['Activated','Deactivated','Other']:
    if col not in daily.columns: daily[col] = 0

# Build table dynamically — fixed cols + whatever status cols exist
fixed_left  = ['User Name','Area','shift_date','checkin','checkout','shift_hours',
               'checkin_to_first_min','last_to_checkout_min']
fixed_right = ['Total','Success %','Swaps','Activated','Deactivated']
status_cols = [c for c in ['Success','Failed','closed','Other'] if c in daily.columns]
all_cols    = fixed_left + status_cols + fixed_right
table = daily[all_cols].copy()

table['shift_date'] = pd.to_datetime(table['shift_date']).dt.strftime('%d %B %A')
table['checkin']    = pd.to_datetime(table['checkin'], errors='coerce').dt.strftime('%d %b %H:%M').fillna('-')
no_checkout = table['checkout'].isna()
table['checkout']   = pd.to_datetime(table['checkout'], errors='coerce').dt.strftime('%d %b %H:%M').fillna('Missed')
table['shift_hours'] = table['shift_hours'].apply(
    lambda x: int(x) if pd.notna(x) and float(x) == int(float(x)) else round(float(x), 1) if pd.notna(x) else 'Missed'
)
table.loc[no_checkout, 'last_to_checkout_min'] = None

# Rename dynamically to match exact column count
rename_left  = ['Agent','Area','Shift Day','Check In','Check Out','Shift Hrs',
                'Checkin to 1st Action (min)','Last Action to Checkout (min)']
rename_status = [c.capitalize() if c != 'closed' else 'Closed' for c in status_cols]
rename_right = ['Total','Success %','Swaps','Activated','Deactivated']
table.columns = rename_left + rename_status + rename_right
table = table.sort_values(['Shift Day','Area','Agent']).reset_index(drop=True)

def color_success(val):
    if isinstance(val, (int, float)):
        if val >= 75: return 'color: #3fb950; font-weight: bold'
        if val >= 50: return 'color: #e3b341; font-weight: bold'
        if val > 0:   return 'color: #f85149; font-weight: bold'
    return ''

st.dataframe(table.style.applymap(color_success, subset=['Success %']), use_container_width=True, height=430, column_config={"Agent": st.column_config.TextColumn("Agent", pinned=True)})


# ── TABS ──────────────────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4 = st.tabs([
    "👤 FT Daily Performance",
    "📋 Ops Tasks",
    "📜 User Logs",
    "🗺️ Areas"
])

# ── helpers ───────────────────────────────────────────────────────────────────
def dark_layout(fig, height=420, legend=True, xangle=-35):
    fig.update_layout(
        height=height, paper_bgcolor='#0f1117', plot_bgcolor='#1a1d2e',
        font_color='#e6edf3',
        legend=dict(bgcolor='#1a1d2e') if legend else dict(visible=False),
        showlegend=legend,
        xaxis=dict(tickangle=xangle, gridcolor='#21262d'),
        yaxis=dict(gridcolor='#21262d', tickformat='d'),
        margin=dict(t=30, b=10)
    )
    return fig

def date_filter(key, df, date_col='shift_date'):
    dates  = sorted(df[date_col].dropna().unique())
    fmt    = {d.strftime('%d %b %Y') if hasattr(d,'strftime') else str(d): d for d in dates}
    sel    = st.multiselect("📅 Day", list(fmt.keys()), default=[], key=key)
    return sel, fmt   # return selection + lookup dict

def agent_filter(key, df, name_col='User Name'):
    agents = sorted(df[name_col].dropna().unique().tolist())
    return st.multiselect("👤 Agent", agents, default=[], key=key)

def area_filter(key, df, area_col='Area'):
    areas  = sorted(df[area_col].dropna().unique().tolist())
    return st.multiselect("🗺️ Area", areas, default=[], key=key)

def action_filter(key, df, action_col='Action'):
    actions = sorted(df[action_col].dropna().unique().tolist())
    return st.multiselect("⚡ Action", actions, default=[], key=key)

def status_filter(key, df, status_col='Status'):
    statuses = sorted(df[status_col].dropna().unique().tolist())
    return st.multiselect("📊 Status", statuses, default=[], key=key)

status_colors = {'Success':'#3fb950','Failed':'#f85149','closed':'#8b949e','Other':'#58a6ff'}
logs_raw = pd.read_excel(io.BytesIO(st.session_state['logs_bytes']))
logs_raw['User Name'] = logs_raw['User Name'].str.strip()
logs_raw['ts']        = pd.to_datetime(logs_raw['Date (Local)'], errors='coerce')
logs_raw['date']      = logs_raw['ts'].dt.date

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — FT DAILY PERFORMANCE
# ══════════════════════════════════════════════════════════════════════════════
with tab1:
    f1c1, f1c2, f1c3 = st.columns(3)
    with f1c1:
        t1_day, t1_day_fmt = date_filter('t1_day', daily)
    with f1c2:
        t1_area   = area_filter('t1_area', daily)
    with f1c3:
        t1_agents = agent_filter('t1_agent', daily)

    d1 = daily.copy()
    if t1_day:    d1 = d1[d1['shift_date'].isin([t1_day_fmt[s] for s in t1_day if s in t1_day_fmt])]
    if t1_area:   d1 = d1[d1['Area'].isin(t1_area)]
    if t1_agents: d1 = d1[d1['User Name'].isin(t1_agents)]

    if len(d1) == 0:
        st.info("No data for selected filters.")
    else:
        # 1a — Task outcomes by agent
        st.markdown('<div class="section-title">Task Outcomes by Agent</div>', unsafe_allow_html=True)
        status_cols_avail = [c for c in ['Success','Failed','closed','Other'] if c in d1.columns]
        agg = d1.groupby('User Name')[status_cols_avail+['Total']].sum().reset_index()
        agg = agg[agg['Total']>0].sort_values('Total',ascending=False).head(30)
        labels = agg['User Name'].apply(lambda n: ' '.join(str(n).split()[:2]))
        fig = go.Figure()
        for s in status_cols_avail:
            fig.add_trace(go.Bar(name=s, x=labels, y=agg[s], marker_color=status_colors.get(s,'#8b949e'),
                text=agg[s], textposition='inside'))
        fig.update_layout(barmode='stack')
        st.plotly_chart(dark_layout(fig, 400), use_container_width=True)

        # 1b — Checkin to first action | Last action to checkout
        g1, g2 = st.columns(2)
        with g1:
            st.markdown('<div class="section-title">Checkin → First Action (avg min)</div>', unsafe_allow_html=True)
            gap_in = (d1[d1['checkin_to_first_min']>0]
                .groupby('User Name')['checkin_to_first_min'].mean().round(0).astype(int).reset_index()
                .sort_values('checkin_to_first_min'))
            gap_in['label'] = gap_in['User Name'].apply(lambda n: ' '.join(str(n).split()[:2]))
            if len(gap_in):
                fig = px.bar(gap_in, x='checkin_to_first_min', y='label', orientation='h',
                    color='checkin_to_first_min', color_continuous_scale=['#3fb950','#e3b341','#f85149'],
                    text='checkin_to_first_min', labels={'checkin_to_first_min':'Min','label':''})
                fig.update_traces(textposition='outside')
                st.plotly_chart(dark_layout(fig, 400, legend=False, xangle=0), use_container_width=True)
        with g2:
            st.markdown('<div class="section-title">Last Action → Checkout (avg min)</div>', unsafe_allow_html=True)
            gap_out = (d1[d1['last_to_checkout_min']>0]
                .groupby('User Name')['last_to_checkout_min'].mean().round(0).astype(int).reset_index()
                .sort_values('last_to_checkout_min'))
            gap_out['label'] = gap_out['User Name'].apply(lambda n: ' '.join(str(n).split()[:2]))
            if len(gap_out):
                fig = px.bar(gap_out, x='last_to_checkout_min', y='label', orientation='h',
                    color='last_to_checkout_min', color_continuous_scale=['#3fb950','#e3b341','#f85149'],
                    text='last_to_checkout_min', labels={'last_to_checkout_min':'Min','label':''})
                fig.update_traces(textposition='outside')
                st.plotly_chart(dark_layout(fig, 400, legend=False, xangle=0), use_container_width=True)

        # 1c — Shift hours per agent
        st.markdown('<div class="section-title">Shift Hours per Agent</div>', unsafe_allow_html=True)
        sh = d1.groupby('User Name')['shift_hours'].sum().reset_index()
        sh = sh[sh['shift_hours']>0].sort_values('shift_hours',ascending=False)
        if len(sh):
            sh['color'] = sh['shift_hours'].apply(lambda h: '#3fb950' if h>=8 else ('#e3b341' if h>=4 else '#f85149'))
            sh['label'] = sh['shift_hours'].apply(lambda h: str(int(h)) if float(h)==int(float(h)) else str(round(h,1)))
            sh['name']  = sh['User Name'].apply(lambda n: ' '.join(str(n).split()[:2]))
            fig = px.bar(sh, x='name', y='shift_hours', color='color', color_discrete_map='identity',
                text='label', labels={'name':'','shift_hours':'Hours'})
            fig.update_traces(textposition='outside')
            fig.add_hline(y=8, line_dash='dash', line_color='#e3b341', opacity=0.6, annotation_text='8h')
            st.plotly_chart(dark_layout(fig, 360, legend=False), use_container_width=True)

        # 1d — Swaps & Activate/Deactivate
        s1, s2 = st.columns(2)
        with s1:
            st.markdown('<div class="section-title">Battery Swaps per Agent</div>', unsafe_allow_html=True)
            sw = d1.groupby('User Name')['Swaps'].sum().reset_index()
            sw = sw[sw['Swaps']>0].sort_values('Swaps',ascending=False).head(20)
            if len(sw):
                sw['name'] = sw['User Name'].apply(lambda n: ' '.join(str(n).split()[:2]))
                fig = px.bar(sw, x='name', y='Swaps', text='Swaps',
                    color='Swaps', color_continuous_scale=['#58a6ff','#1f6feb'])
                fig.update_traces(textposition='outside')
                st.plotly_chart(dark_layout(fig, 360, legend=False), use_container_width=True)
        with s2:
            st.markdown('<div class="section-title">Activate / Deactivate per Agent</div>', unsafe_allow_html=True)
            if 'Activated' in d1.columns and 'Deactivated' in d1.columns:
                ad = d1.groupby('User Name')[['Activated','Deactivated']].sum().reset_index()
                ad = ad[(ad['Activated']>0)|(ad['Deactivated']>0)].sort_values('Activated',ascending=False).head(20)
                if len(ad):
                    ad['name'] = ad['User Name'].apply(lambda n: ' '.join(str(n).split()[:2]))
                    ad_m = ad.melt(id_vars='name', value_vars=['Activated','Deactivated'], var_name='Type', value_name='Count')
                    fig = px.bar(ad_m, x='name', y='Count', color='Type', barmode='group',
                        color_discrete_map={'Activated':'#3fb950','Deactivated':'#f85149'},
                        text='Count')
                    fig.update_traces(textposition='outside')
                    st.plotly_chart(dark_layout(fig, 360), use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — OPS TASKS
# ══════════════════════════════════════════════════════════════════════════════
with tab2:
    tasks_df = tasks_raw.copy()
    tasks_df['date'] = pd.to_datetime(tasks_df['Created At'], errors='coerce').dt.date

    f2c1, f2c2, f2c3, f2c4 = st.columns(4)
    with f2c1:
        t2_day, t2_day_fmt = date_filter('t2_day', tasks_df, date_col='date')
    with f2c2:
        t2_area   = area_filter('t2_area', tasks_df)
    with f2c3:
        t2_status = status_filter('t2_status', tasks_df)
    with f2c4:
        t2_agent  = agent_filter('t2_agent', tasks_df)

    if t2_day:    tasks_df = tasks_df[tasks_df['date'].isin([t2_day_fmt[s] for s in t2_day if s in t2_day_fmt])]
    if t2_area:   tasks_df = tasks_df[tasks_df['Area'].isin(t2_area)]
    if t2_status: tasks_df = tasks_df[tasks_df['Status'].isin(t2_status)]
    if t2_agent:  tasks_df = tasks_df[tasks_df['User Name'].isin(t2_agent)]

    if len(tasks_df) == 0:
        st.info("No data for selected filters.")
    else:
        # KPIs
        k1,k2,k3,k4,k5 = st.columns(5)
        k1.metric("Total Tasks",   len(tasks_df))
        k2.metric("Success",       int((tasks_df['Status']=='Success').sum()))
        k3.metric("Failed",        int((tasks_df['Status']=='Failed').sum()))
        k4.metric("Closed",        int((tasks_df['Status']=='closed').sum()))
        k5.metric("Avg Duration",  f"{tasks_df['Total Duration (minutes)'].dropna().mean():.0f} min" if tasks_df['Total Duration (minutes)'].notna().any() else "—")
        st.markdown("---")

        t2a, t2b = st.columns(2)
        with t2a:
            # Tasks by date
            st.markdown('<div class="section-title">Tasks by Date</div>', unsafe_allow_html=True)
            by_date = tasks_df.groupby(['date','Status']).size().reset_index(name='count')
            by_date['date_fmt'] = pd.to_datetime(by_date['date']).dt.strftime('%d %b')
            if len(by_date):
                fig = px.bar(by_date, x='date_fmt', y='count', color='Status', barmode='stack',
                    color_discrete_map=status_colors, text_auto=True)
                fig.update_traces(textposition='inside')
                st.plotly_chart(dark_layout(fig, 380, xangle=0), use_container_width=True)
        with t2b:
            # Tasks by area
            st.markdown('<div class="section-title">Tasks by Area</div>', unsafe_allow_html=True)
            by_area = tasks_df.groupby(['Area','Status']).size().reset_index(name='count')
            if len(by_area):
                fig = px.bar(by_area, x='Area', y='count', color='Status', barmode='stack',
                    color_discrete_map=status_colors, text_auto=True)
                fig.update_traces(textposition='inside')
                st.plotly_chart(dark_layout(fig, 380, xangle=0), use_container_width=True)

        t2c, t2d = st.columns(2)
        with t2c:
            # Top agents by task count
            st.markdown('<div class="section-title">Tasks by Agent</div>', unsafe_allow_html=True)
            by_agent = tasks_df.groupby(['User Name','Status']).size().reset_index(name='count')
            totals   = by_agent.groupby('User Name')['count'].sum().nlargest(25).index
            by_agent = by_agent[by_agent['User Name'].isin(totals)]
            by_agent['label'] = by_agent['User Name'].apply(lambda n: ' '.join(str(n).split()[:2]))
            if len(by_agent):
                fig = px.bar(by_agent, x='label', y='count', color='Status', barmode='stack',
                    color_discrete_map=status_colors, text_auto=True)
                fig.update_traces(textposition='inside')
                st.plotly_chart(dark_layout(fig, 400), use_container_width=True)
        with t2d:
            # Duration distribution
            st.markdown('<div class="section-title">Task Duration Distribution (min)</div>', unsafe_allow_html=True)
            dur = tasks_df['Total Duration (minutes)'].dropna()
            if len(dur):
                fig = px.histogram(dur, nbins=30, color_discrete_sequence=['#58a6ff'],
                    labels={'value':'Duration (min)','count':'Tasks'})
                st.plotly_chart(dark_layout(fig, 400, legend=False, xangle=0), use_container_width=True)

        # Auto-failed breakdown
        st.markdown('<div class="section-title">Auto-Failed vs Manual Failed</div>', unsafe_allow_html=True)
        failed_df = tasks_df[tasks_df['Status']=='Failed'].copy()
        if len(failed_df):
            failed_df['Type'] = failed_df['Auto Failed'].apply(lambda x: 'Auto Failed' if str(x).lower() in ('true','1','yes') else 'Manual Failed')
            af_area = failed_df.groupby(['Area','Type']).size().reset_index(name='count')
            fig = px.bar(af_area, x='Area', y='count', color='Type', barmode='group',
                color_discrete_map={'Auto Failed':'#f85149','Manual Failed':'#e3b341'},
                text='count')
            fig.update_traces(textposition='outside')
            st.plotly_chart(dark_layout(fig, 360, xangle=0), use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — USER LOGS
# ══════════════════════════════════════════════════════════════════════════════
with tab3:
    log_df = logs_raw.copy()

    f3c1, f3c2, f3c3, f3c4 = st.columns(4)
    with f3c1:
        t3_day, t3_day_fmt = date_filter('t3_day', log_df, date_col='date')
    with f3c2:
        t3_area   = area_filter('t3_area', log_df)
    with f3c3:
        t3_action = action_filter('t3_action', log_df)
    with f3c4:
        t3_agent  = agent_filter('t3_agent', log_df)

    if t3_day:    log_df = log_df[log_df['date'].isin([t3_day_fmt[s] for s in t3_day if s in t3_day_fmt])]
    if t3_area:   log_df = log_df[log_df['Area'].isin(t3_area)]
    if t3_action: log_df = log_df[log_df['Action'].isin(t3_action)]
    if t3_agent:  log_df = log_df[log_df['User Name'].isin(t3_agent)]

    if len(log_df) == 0:
        st.info("No data for selected filters.")
    else:
        lk1,lk2,lk3 = st.columns(3)
        lk1.metric("Total Events", f"{len(log_df):,}")
        lk2.metric("Agents",       log_df['User Name'].nunique())
        lk3.metric("Actions",      log_df['Action'].nunique())
        st.markdown("---")

        l3a, l3b = st.columns(2)
        with l3a:
            st.markdown('<div class="section-title">Events by Action Type</div>', unsafe_allow_html=True)
            by_action = log_df.groupby('Action').size().reset_index(name='count').sort_values('count',ascending=True)
            fig = px.bar(by_action, x='count', y='Action', orientation='h',
                color='count', color_continuous_scale=['#1f6feb','#58a6ff'],
                text='count', labels={'count':'Events','Action':''})
            fig.update_traces(textposition='outside')
            st.plotly_chart(dark_layout(fig, 420, legend=False, xangle=0), use_container_width=True)
        with l3b:
            st.markdown('<div class="section-title">Events by Date & Action</div>', unsafe_allow_html=True)
            by_date_action = log_df.groupby(['date','Action']).size().reset_index(name='count')
            by_date_action['date_fmt'] = pd.to_datetime(by_date_action['date']).dt.strftime('%d %b')
            # Show top 6 actions only to avoid clutter
            top_actions = log_df['Action'].value_counts().head(6).index.tolist()
            by_date_action = by_date_action[by_date_action['Action'].isin(top_actions)]
            if len(by_date_action):
                fig = px.bar(by_date_action, x='date_fmt', y='count', color='Action', barmode='stack',
                    text_auto=True, labels={'count':'Events','date_fmt':''})
                fig.update_traces(textposition='inside')
                st.plotly_chart(dark_layout(fig, 420, xangle=0), use_container_width=True)

        l3c, l3d = st.columns(2)
        with l3c:
            st.markdown('<div class="section-title">Top Agents by Events</div>', unsafe_allow_html=True)
            by_agent_log = log_df.groupby('User Name').size().reset_index(name='count').sort_values('count',ascending=False).head(25)
            by_agent_log['label'] = by_agent_log['User Name'].apply(lambda n: ' '.join(str(n).split()[:2]))
            fig = px.bar(by_agent_log, x='label', y='count',
                color='count', color_continuous_scale=['#1f6feb','#58a6ff'],
                text='count', labels={'label':'','count':'Events'})
            fig.update_traces(textposition='outside')
            st.plotly_chart(dark_layout(fig, 400, legend=False), use_container_width=True)
        with l3d:
            st.markdown('<div class="section-title">Events by Area</div>', unsafe_allow_html=True)
            by_area_log = log_df.groupby(['Area','Action']).size().reset_index(name='count')
            top_a = log_df['Action'].value_counts().head(6).index.tolist()
            by_area_log = by_area_log[by_area_log['Action'].isin(top_a)]
            fig = px.bar(by_area_log, x='Area', y='count', color='Action', barmode='stack',
                text_auto=True, labels={'count':'Events','Area':''})
            fig.update_traces(textposition='inside')
            st.plotly_chart(dark_layout(fig, 400, xangle=0), use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — AREAS
# ══════════════════════════════════════════════════════════════════════════════
with tab4:
    f4c1, f4c2, f4c3 = st.columns(3)
    with f4c1:
        t4_day, t4_day_fmt = date_filter('t4_day', daily)
    with f4c2:
        t4_area       = area_filter('t4_area', daily)
    with f4c3:
        t4_log_action = action_filter('t4_log_action', logs_raw)

    d4 = daily.copy()
    if t4_day:  d4 = d4[d4['shift_date'].isin([t4_day_fmt[s] for s in t4_day if s in t4_day_fmt])]
    if t4_area: d4 = d4[d4['Area'].isin(t4_area)]

    log4 = logs_raw.copy()
    if t4_day:        log4 = log4[log4['date'].isin([t4_day_fmt[s] for s in t4_day if s in t4_day_fmt])]
    if t4_area:       log4 = log4[log4['Area'].isin(t4_area)]
    if t4_log_action: log4 = log4[log4['Action'].isin(t4_log_action)]

    tasks4 = tasks_raw.copy()
    tasks4['date'] = pd.to_datetime(tasks4['Created At'], errors='coerce').dt.date
    if t4_day:  tasks4 = tasks4[tasks4['date'].isin([t4_day_fmt[s] for s in t4_day if s in t4_day_fmt])]
    if t4_area: tasks4 = tasks4[tasks4['Area'].isin(t4_area)]

    if len(d4) == 0 and len(log4) == 0:
        st.info("No data for selected filters.")
    else:
        a4a, a4b = st.columns(2)
        with a4a:
            st.markdown('<div class="section-title">Task Outcomes by Area</div>', unsafe_allow_html=True)
            sc_avail = [c for c in ['Success','Failed','closed','Other'] if c in d4.columns]
            area_tasks = d4.groupby('Area')[sc_avail].sum().reset_index()
            area_tasks = area_tasks[area_tasks['Area'].notna() & (area_tasks['Area']!='')]
            area_m = area_tasks.melt(id_vars='Area', value_vars=sc_avail, var_name='Status', value_name='count')
            area_m = area_m[area_m['count']>0]
            if len(area_m):
                fig = px.bar(area_m, x='Area', y='count', color='Status', barmode='stack',
                    color_discrete_map=status_colors, text_auto=True)
                fig.update_traces(textposition='inside')
                st.plotly_chart(dark_layout(fig, 380, xangle=0), use_container_width=True)
        with a4b:
            st.markdown('<div class="section-title">Log Events by Area</div>', unsafe_allow_html=True)
            area_logs = log4.groupby(['Area','Action']).size().reset_index(name='count')
            top_a4 = log4['Action'].value_counts().head(6).index.tolist()
            area_logs = area_logs[area_logs['Action'].isin(top_a4)]
            if len(area_logs):
                fig = px.bar(area_logs, x='Area', y='count', color='Action', barmode='stack',
                    text_auto=True, labels={'count':'Events'})
                fig.update_traces(textposition='inside')
                st.plotly_chart(dark_layout(fig, 380, xangle=0), use_container_width=True)

        a4c, a4d = st.columns(2)
        with a4c:
            st.markdown('<div class="section-title">Agents per Area</div>', unsafe_allow_html=True)
            agents_area = d4.groupby('Area')['User Name'].nunique().reset_index(name='Agents').sort_values('Agents',ascending=False)
            if len(agents_area):
                fig = px.bar(agents_area, x='Area', y='Agents', text='Agents',
                    color='Agents', color_continuous_scale=['#1f6feb','#58a6ff'])
                fig.update_traces(textposition='outside')
                st.plotly_chart(dark_layout(fig, 360, legend=False, xangle=0), use_container_width=True)
        with a4d:
            st.markdown('<div class="section-title">Task Outcomes by Area & Date</div>', unsafe_allow_html=True)
            area_date = tasks4.groupby(['Area','Status']).size().reset_index(name='count')
            if len(area_date):
                fig = px.bar(area_date, x='Area', y='count', color='Status', barmode='group',
                    color_discrete_map=status_colors, text='count')
                fig.update_traces(textposition='outside')
                st.plotly_chart(dark_layout(fig, 360, xangle=0), use_container_width=True)

        # Area trend over time
        st.markdown('<div class="section-title">Daily Task Volume by Area</div>', unsafe_allow_html=True)
        area_trend = tasks4.groupby(['date','Area']).size().reset_index(name='count')
        area_trend['date_fmt'] = pd.to_datetime(area_trend['date']).dt.strftime('%d %b')
        if len(area_trend):
            fig = px.line(area_trend, x='date_fmt', y='count', color='Area',
                markers=True, labels={'count':'Tasks','date_fmt':''})
            st.plotly_chart(dark_layout(fig, 360, xangle=0), use_container_width=True)
