"""
Stop-Grid Trader – Slot-managed Grid + Majority-side Pair-Net + Stuck-aware + Auto Step (v1.8)
# ----------------------------------------------------------------------------------------------
仕様の要点:
- グリッド幅: 既定は spread*2。BTCなど極端スプレッドは自動調整可
- スロット管理:
    * スロット=各グリッド段の価格帯（幅= step * slot_width_mult）
    * 既存の「ポジション」と「未約定」がスロットを占有 → 新規は空きスロットだけに配置
    * 巻き直しも同様。重複/はみ出し未約定は少数ずつだけクリーンアップ（負荷抑制）
- 相殺(Pair-Net):
    * 「ポジ本数が多い方向」を“担がれ方向”とみなし、同方向の中から最不利(worst)を選択
    * 反対方向（少ない方向）の“勝ちポジ”を集め、net >= 0.2 USD で相殺
    * 近傍アーミング + stuck-aware（逆行が複数本＆担がれ幅>=spread*2で事前TP外し）
    * CLOSE_BY優先。非対応/失敗は成行にフォールバック
- 送信: シンボルfilling_mode優先、Unsupportedは RETURN→IOC→FOK リトライ
- 安全上限: ポジ数/未約定/総ロット/スプレッド/ドローダウン/レート/リセンタ頻度
"""

from __future__ import annotations
import tkinter as tk
from tkinter import ttk, messagebox
import threading, time, sys, os, queue, math
import MetaTrader5 as mt5

# [FIX] Thread Safety Locks
_LOG_LOCK = threading.Lock()
_MT5_LOCK = threading.Lock()
from collections import defaultdict
import logging
import json
import csv
import requests
import uuid

try:
    import psutil
except ImportError:
    psutil = None

# ── optional ML (CatBoost) ────────────────────────────────────
try:
    from catboost import CatBoostClassifier  # type: ignore
    _HAS_CATBOOST = True
except Exception:
    CatBoostClassifier = None  # type: ignore
    _HAS_CATBOOST = False

# ── defaults ──────────────────────────────────────────────────
DEF_SYMBOL        = "XAUUSD"
DEF_DIGITS        = 2
DEF_LOT           = 0.01
DEF_ORDERS_SIDE   = 10
# ── constants ─────────────────────────────────────────────────
DEVIATION         = 100
# デフォルトで他EAと衝突しないよう非0のMagicを採用
MAGIC_NUMBER      = 410001
GRID_TAG          = "recenter grid"
CHECK_INTERVAL    = 0.2
PRICE_EPS_FACTOR  = 0.5    # price match epsilon = point * this
MARKET_STALE_TICK_SEC = 30.0
# ── pair-net tuning ───────────────────────────────────────────
PAIRNET_ENABLE            = False
PAIRNET_COOLDOWN_SEC      = 1.0
PAIRNET_MIN_NET_PROFIT    = 0.4    # ★ 合計 +$0.2 以上で相殺
PAIRNET_MAX_POS_TO_USE    = 4
PAIRNET_ARM_RATIO         = 0.75
PAIRNET_DISARM_RATIO      = 0.40
USE_CLOSE_BY              = True
LOG_FILE                  = os.path.join(os.path.dirname(__file__) or ".", "pairnet_log.txt")
# ── safety guards ────────────────────────────────────────────
MAX_POSITIONS             = 9000
MAX_VOLUME                = 50.0
MAX_PENDING               = 120
MAX_ORDERS_PER_MIN        = 60
MAX_RECENTER_PER_MIN      = 6
DD_STOP_PCT               = 90.0
SPREAD_MAX_PTS            = 9000
HARD_STOP_EQUITY          = None
# ── anti-chop guard（反転多発のDD深掘り対策） ─────────────────────
CHOP_GUARD_ENABLE         = True
CHOP_FLIP_TF              = "M15"     # 反転検知に使うTF
CHOP_FLIP_WINDOW_SEC      = 10 * 60   # この時間窓で
CHOP_FLIP_COUNT           = 3         # 反転がこれ以上なら
CHOP_BLOCK_SEC            = 5 * 60    # 新規エントリーを止める秒数
CHOP_CANCEL_PENDINGS      = True      # ブロック開始時に未約定を削除
# ── ML gate（Pivotの新規エントリー可否判定） ──────────────────────
AI_ENABLE                 = True
AI_MODEL_PATH             = "anyabot_gate.cbm"  # ここに学習済みモデルを置く（同階層）
AI_THRESHOLD              = 0.50                # p(良) >= 閾値でエントリー許可
AI_FAIL_OPEN              = True                # 予測失敗時に True=許可 / False=拒否
AI_APPLY_WHEN_CLOSE_LT    = 2                   # 直近window内の決済がこれ未満のときだけAI判定を強める（=効いてない相場の抑制）
AI_LOG_ENABLE             = True                # 学習用ログ（特徴量）を出す
AI_LOG_FILE               = os.path.join(os.path.abspath(os.path.dirname(__file__) or "."), "anyabot_ai_log.csv")
AI_DEBUG                  = False

# ── state log（DD/回転のラベル付け用） ──────────────────────────
STATE_LOG_ENABLE          = True
STATE_LOG_INTERVAL_SEC    = 5.0
STATE_LOG_FILE            = os.path.join(os.path.abspath(os.path.dirname(__file__) or "."), "anyabot_state_log.csv")
DEBUG_LOG_FILE            = os.path.join(os.path.abspath(os.path.dirname(__file__) or "."), "anyabot_debug.log")

def _debug_log(msg: str):
    """Write debug message to separate log file, avoiding stdout"""
    try:
        import datetime
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        with open(DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass

# ── entry budget（決済回転率ベースの自然なエントリー制限） ─────────
ENTRY_BUDGET_ENABLE       = False  # Disabled for Pivot Strict (high precision, low frequency)
ENTRY_BUDGET_WINDOW_SEC   = 60 * 60            # 30->60 (停滞時の積み増し抑制)
ENTRY_BUDGET_BASE         = 2                  # 最低でもこの回数までは許可 (1->2)
ENTRY_BUDGET_PER_CLOSE    = 1                  # 直近決済1回につき許可枠を増やす
ENTRY_BUDGET_CAP          = 20                 # 上限 30->20
ENTRY_BUDGET_BYPASS_MINOR = False              # buy/sell偏り時、少数側は制限中でも許可（相殺追従用）
ENTRY_BUDGET_BYPASS_UNDER_POS = 1              # 保有数が少ない間は budget を無視 (0本時はバイパス)
ENTRY_BUDGET_TREND_BONUS      = 2              # H1トレンド方向なら予算枠にボーナス加算

# ── 1M Pair-Net Entry（DD改善用のヘッジ補助エントリー） ─────────
M1_PAIRNET_ENABLE     = True   # Nanpin中のみ + Smart Close filter適用で運用
M1_PAIRNET_CD_SEC     = 45.0        # エントリー間のクールダウン
M1_PAIRNET_LOT_MULT   = 1.0         # メインロットと同一
M1_PAIRNET_HEDGE_LIMIT = False      # True: 少数側のみヘッジ許可（差分まで）, False: 制限なし
M1_PAIRNET_DD1_FILTER = True        # dd1（強弱）フィルタを有効化
M1_PAIRNET_DD1_MULT = 0.5           # 閾値 = スプレッド × この倍率（動的）
MAX_TOTAL_POSITIONS   = 900         # 全体ポジション上限 (安全弁・緩和済み)

# ── Offset Re-entry（相殺後の再エントリー）モード設定 ─────────
# "NONE"     = 再エントリーなし（相殺のみ）
# "LEGACY"   = 従来通り（閉じた勝ちポジ数だけ再エントリー）+ Entry Budget加算
# "BALANCED" = バランス型（相手側ポジ数 + バッファまで許可）+ Entry Budget加算なし
# "DIRECT"   = 削減型（used_extra = Boss除く消費分のみ再発注）。確実に1本減らす。
OFFSET_REENTRY_MODE = "BALANCED"

# BALANCEDモード時のバッファ設定
# 動的バッファ: min(base + floor(total_positions / divisor), max_buffer)
OFFSET_REENTRY_BUFFER_BASE = 1      # 基本バッファ
OFFSET_REENTRY_BUFFER_DIVISOR = 5   # 総ポジ数をこれで割ってバッファに加算
OFFSET_REENTRY_BUFFER_MAX = 4       # バッファ上限

# ── Pivot Stream 1ポジ目のエントリーフィルター ─────────
# 1ポジ目は厳格な条件を満たす必要がある（追加ポジはより緩い）
PIVOT_FIRST_REQUIRE_BODY_BREAK = True  # 1m実体抜けを必須にする
PIVOT_FIRST_REQUIRE_H4_TREND = True    # H4トレンド方向一致を必須にする
PIVOT_FIRST_REQUIRE_M5_BODY_BREAK = False  # M5実体抜けも必須にする（オプション）
PIVOT_FIRST_REQUIRE_REF2_BODY_BREAK = False  # 環境認識足の実体抜けを必須にする
PIVOT_FIRST_REQUIRE_UPPER_BODY_BREAK = True  # 監視足(M1→M15, M5→H1, M15→H4)の実体抜けを必須にする
PIVOT_FIRST_ENTRY_RELAX_ENABLE = False  # Trueで1ポジ目の厳格チェーンをスキップ
PIVOT_FIRST_BODY_BREAK_ONLY_ENABLE = False  # Trueで実体抜けは1ポジ目のみ必須（Streamは免除）

# ── Zigzag Entry Permission ─────────
PIVOT_ZIGZAG_ENTRY_ENABLE = False  # Zigzagエントリー許可システム
PIVOT_ZIGZAG_BARS = 100            # Zigzag計算に使うバー数

# ── Nanpin Hedge (段階的比率方式 + M1確定) ─────────
NANPIN_FULL_HEDGE_ENABLE = False   # ナンピンヘッジ有効化
NANPIN_HEDGE_COOLDOWN_SEC = 60    # ヘッジ間クールダウン (秒)
# 段階的ヘッジ比率: (dominant_vol / base_lot の倍率上限, ヘッジ比率)
# ≤3倍: 0%  (通常の押し目/戻り → ヘッジ不要)
# 4-6倍: 20% (軽度ナンピン → 軽いヘッジ)
# 7-10倍: 30% (深いナンピン → 中度ヘッジ)
# 11倍以上: 40% (非常に深い → 最大ヘッジ)
NANPIN_HEDGE_TIERS = ((3, 0.0), (6, 0.20), (10, 0.30))
NANPIN_HEDGE_MAX_RATIO = 0.40

# ── Nanpin Prevention (ナンピン防止ガード) ─────────
NANPIN_PREVENT_ENABLE = False      # ナンピン防止ガード有効化

# ── 多数派ナンピン制限フィルター ─────────────────────────────────────────
# ポジション数ベースで多数派サイドへの追加エントリーをブロック（PnL条件なし）
# ※既存の NANPIN_PREVENT_ENABLE（PnLベース）とは独立して動作（両方ONも可）
MAJORITY_NANPIN_FILTER_ENABLE   = True   # 多数派ナンピンフィルター有効化
MAJORITY_NANPIN_FILTER_MIN_DIFF = 2      # この本数差以上で多数派側をブロック

# ── Pivot Strict Mode ─────────
# True: 2TF一致（Exec + C2のみ、C1スキップ）→ エントリー機会増加
# False: 3TF一致（Exec + C1 + C2全て）→ 従来の高精度モード
PIVOT_STRICT_SKIP_C1 = False

# ── Offset BALANCED追加設定 ─────────
OFFSET_BALANCED_COUNTS_BUDGET = False  # BALANCEDでもEntry Budget加算
OFFSET_REENTRY_MIN = 1                 # 最低再エントリー本数（0許可を防ぐ）
OFFSET_RETRY_TTL_SEC = 60              # リトライキューの有効期限（秒）

# ── 動的Offsetモード切替設定 v10.3 ─────────
# Boss保有時間/停滞時間に応じて動的切替
OFFSET_DYNAMIC_MODE = True             # 動的モード切替を有効化

# ★v10.3: 段階的切替 (LEGACY←→BALANCED←→DIRECT)
# Boss保有長/停滞長 → LEGACY（早期相殺優先）
# 不均衡 → BALANCED（均衡回復）
# ポジ過多 → DIRECT（強制縮小）

# Boss保有時間閾値（分）
OFFSET_BOSS_AGE_LEGACY = 30            # Boss保有これ以上でLEGACY

# 停滞時間閾値（分）
OFFSET_STAG_LEGACY = 15                # これ以上停滞でLEGACY

# 不均衡閾値（Buy/Sellの差）
OFFSET_IMBAL_BALANCED = 5              # これ以上でBALANCED
OFFSET_IMBAL_DIRECT = 10               # これ以上でDIRECT

# ポジション過多閾値
OFFSET_OVEREXTEND_THRESHOLD = 6        # これ以上でDIRECT強制


# 相殺後の「再構築弾」（offset-entry）が、entry budget 等と重なって過剰にならないための上限。
# used_extra+1 が大きくなり得るため、まずは安全側に抑える（必要なら大きくする）。
OFFSET_REBUILD_MAX_ORDERS = 50
# offset-entry の上限を entry budget から動的に算出する
OFFSET_REBUILD_DYNAMIC_LIMIT = False
# entry budget に少し上乗せして offset-entry を許可する
OFFSET_REBUILD_BUDGET_BONUS = 5
# protective（少数側バイパス）時の追加バッファ
OFFSET_REBUILD_PROTECTIVE_BONUS = 1
# 救済エントリーの上限 (これ以上はRescue発動しない)
RESCUE_ENTRY_CAP = 12

# Profit Recycling / Offset Tuning
RECYCLE_BUDGET_THRESHOLD    = 1     # 残りBudgetがこれ以下なら「生存モード」として相殺回転させる
RECYCLE_PROXIMITY_RATIO     = 0.8   # Bossを80%カバーできるなら、生存モードでも相殺我慢して貯金する
RECYCLE_MIN_NET_PROFIT      = 0.0   # 相殺時の再低純利益（0=トントンでも回転優先）
PROTECTIVE_MODE_BONUS       = 0     # 少数派(Loser)保護モードのボーナス枠（無限ではなく上限を設ける）

# ═══════════════════ v10 NEW FEATURES ═══════════════════
# ── 1. 回転率ベースのエントリー制限 ─────────────────────────
# 回転率 = 決済数 / エントリー数 が低いとロジックが効いていない判断
TURNOVER_RATE_ENABLE        = True    # 回転率制限を有効化
TURNOVER_RATE_MIN           = 0.3     # これ未満だとエントリーを抑制
TURNOVER_RATE_PENALTY       = 2       # 低回転時にlimitからこれを引く

# ── 2. 偏り時の決済慎重化 ─────────────────────────────────
# 少数側のポジションを決済する際は閾値を厳しくする
MINORITY_CLOSE_CAUTION      = True    # 少数側決済慎重化を有効化
MINORITY_CLOSE_MULT         = 1.5     # 少数側決済時の利益閾値倍率

# ── 3. 決済中の逆行チェック＆中断 ─────────────────────────────
# 複数ポジション決済時、1ポジごとにnetを再計算し閾値割れなら中断
CLOSE_ABORT_ON_REVERSAL     = True    # 逆行中断を有効化
CLOSE_MIN_NET_THRESHOLD     = 0.5     # 最低確保利益（これ以下なら中断。マイナス決済を防ぐ）

# ── 4. 利益温存ポジションの確保 ─────────────────────────────
# 異方向相殺において、利益の乗っているポジションを保護して伸ばす
PROFIT_PRESERVE_ENABLE      = False    # 利益温存を有効化
# ※保護本数は動的に決定されます (min(2, len-2))

# ── 5. 多数派含み益ポジションのロック ────────────────────────────────────
# 多数派（ポジション数が多い側）が含み益を持つ場合、決済対象から除外して利益を伸ばす
MAJORITY_PROFIT_LOCK_ENABLE   = True   # 多数派含み益ロック有効化
MAJORITY_PROFIT_LOCK_TOP_N    = 2      # 多数派の含み益上位N本をロック（0=閾値のみ）
MAJORITY_PROFIT_LOCK_MIN_PIPS = 0.0   # ロック対象の最低含み益（ドル。0=利益があれば全部対象）
MAJORITY_PROFIT_LOCK_MIN_DIFF = 1      # 多数派判定の最低差（この差以上で多数派とみなす）

# ── 動的調整 (3軸スコアリング) ────────────────────────────────────────────
# MAJORITY_NANPIN_FILTER_MIN_DIFF / MAJORITY_PROFIT_LOCK_TOP_N を市場状態で自動補正
MAJORITY_DYNAMIC_ENABLE   = True   # True: 動的調整ON / False: 上記静的値をそのまま使用

# トレンド一致スコア閾値（M1/M5/M15/H1 の中で多数派方向と一致するTF数）
MAJORITY_TREND_STRONG_TH  = 3      # この本数以上一致 → 強トレンド一致 (緩和方向)
MAJORITY_TREND_WEAK_TH    = 1      # この本数以上一致 → 弱トレンド一致 (小幅緩和)

# 偏り比率閾値（buy/sell本数差）
MAJORITY_IMBAL_HIGH_TH    = 7      # この本数差以上 → 高偏り (大きく厳格化)
MAJORITY_IMBAL_MID_TH     = 5      # この本数差以上 → 中偏り (小幅厳格化)

# 含み損深度閾値（多数派サイドの含み損合計ドル）
MAJORITY_LOSS_DEEP_USD    = 30.0   # この損失以上 → 深い損失 (大きく厳格化)
MAJORITY_LOSS_MID_USD     = 10.0   # この損失以上 → 中程度損失 (小幅厳格化)
# ═════════════════════════════════════════════════════════

# ── v10.3: Offset Disable Safeguard ───────────────────────
# Offset無効化時の安全装置（無限ナンピン防止）
# 損益不均衡比率 = abs(BuyPnL - SellPnL) / (abs(BuyPnL) + abs(SellPnL))
# これが閾値を超えたら「負けている側」のエントリーをブロック
OFFSET_DISABLE_IMBAL_RATIO = 0.6
OFFSET_DISABLE_IMBAL_GUARD_ENABLE = False

# offset-entry を entry budget に含めるか（推奨: False）
# - entry budget は「シグナル（pivot）で入る頻度」を回転率ベースで制御する目的
# - offset-entry は「ポジション管理（相殺後の再構築）」なので、別枠で抑える方が破綻しにくい
OFFSET_COUNTS_TOWARD_ENTRY_BUDGET = True

# offset-entry を buy/sell バランスで制御（推奨: True）
# - 現在の保有バランスを悪化させる side の offset-entry は抑制する
OFFSET_BALANCE_ONLY_IF_MINORITY_OR_EQUAL = False

# 決済カウントの考え方：
# 相殺などで「同じループ内に複数ポジが決済」されても、回転（市場の良さ）としては 1 回に数えたい場合がある。
# そのため、決済イベントを短時間は“同一イベント”として扱い close_hist への加算を抑制する。
CLOSE_EVENT_DEDUP_SEC = 0.0
# ── step auto-tuning (BTC等) ──────────────────────────────────
STEP_MODE_DEFAULT         = "spread2"   # "auto"|"spread2"|"percent"|"abs_usd"|"fixed_pts"
RECOMPUTE_ON_RECENTER     = True
SPREAD_CAP_PTS            = 400
STEP_PTS_MIN_USER         = 5
STEP_PTS_MAX_USER         = 5000
STEP_PRICE_PCT            = 0.00015  # 0.015%
STEP_ABS_USD              = 5.0
FIXED_STEP_PTS            = None
MIN_TP_SPREAD_MULT        = 2.0      # TP下限 = spread * 係数



# ── Spread Auto Tuning ───────────────────────────────────────
SPREAD_FILTER_AUTO        = True     # スプレッド自動追従
SPREAD_FILTER_EMA_ALPHA   = 0.05     # 平均化の重み (小さいほどゆっくり)
SPREAD_FILTER_MULTIPLIER  = 2.0      # 平均の何倍まで許容するか
SPREAD_FILTER_MIN_PTS     = 50       # 自動計算時の下限 (これより狭くはしない)
# ── slot management ──────────────────────────────────────────
SLOT_ENABLE               = True
SLOT_WIDTH_MULT           = 1.0      # スロット幅 = step * この値（1.0 = 1段ぶん）
SLOT_CLEANUP_PER_RECENTER = 5        # 1回の巻き直しでキャンセルする上限（過去注文の整理）
MIN_ENTRY_DISTANCE_MULT   = 0.5      # 最小距離ガード: 同一サイドの直近エントリ価格±(step*0.6)内は新規禁止

# ── stuck-aware（事前TP外し） ────────────────────────────────
STUCK_ENABLE              = True
STUCK_MIN_LOSERS          = 2
STUCK_ADVERSE_MULT        = 5.5      # 担がれ幅 >= spread*2
STUCK_ARM_MAX_WINNERS     = 4
STUCK_ARM_COOLDOWN_SEC    = 3.0
# ── Market Close (Entry Block) ──────────────────────────────
WEEKEND_BLOCK_HOUR        = 25       # 金曜日のこの時間(Server)以降は新規停止 (無効化)
DAILY_BLOCK_HOUR          = 25       # 毎日のこの時間(Server)以降は新規停止 (無効化)
# ── Volatility Guard (ATR-based) ────────────────────────────
VOLATILITY_GUARD_ENABLE   = False
VOLATILITY_ATR_PERIOD     = 14       # ATR計算期間
VOLATILITY_ATR_MULT       = 2.0      # 直近ATR / 平均ATR がこの倍率を超えたらブロック
VOLATILITY_AVG_WINDOW     = 100      # 「通常のATR」を算出する平均化期間
# ── Time-based Entry Block ──────────────────────────────────
BLOCK_TIME_ENABLE         = False    # 時間帯ブロックを有効化
BLOCK_TIME_START_HOUR     = 0        # ブロック開始 (Server時刻)
BLOCK_TIME_END_HOUR       = 1        # ブロック終了 (Server時刻)
# ── One-sided Offset Cap ────────────────────────────────────
OFFSET_ONE_SIDED_CAP      = 100        # 片側のみの時の最大エントリー数（実質無効化: 100）
# ─────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
# ── logging control ──────────────────────────────────────────
LOG_VERBOSITY      = 1   # 0=quiet / 1=events(推奨) / 2=debug
LOG_TO_FILE        = True
LOG_MAX_SIZE_KB    = 10240
LOG_RATE_LIMIT_SEC = 2.0
STATUS_PRINT_STDOUT = False  # GUI運用時に [STATUS] を stdout に出さない

# タグごとの“最低verbosity”。この値未満ならそのタグは黙る（余計なログ抑制用）
TAG_MIN_VERBOSITY = {
    "DIR": 2,    # [DIRDBG] など
    "TFLOW": 2,  # trace/DBG
    "WD": 2,     # watchdog
    "ORD": 2,    # 注文送信トレース
    "MON": 2,    # 監視系の細かいログ
}

# ### PATCH: TERM (Equity Ladder) constants
TERM_ENABLE            = False      # ラダー有効/無効
TERM_USE_EQUITY        = True      # True=equity基準 / False=balance基準
TERM_STEP_USD          = 50.0      # term幅（例: +50USDで全決済→次termへ）
TERM_MIN_HOLD_SEC      = 90.0      # term開始直後の即全決済を避ける最小保有時間
TERM_COOLDOWN_SEC      = 20.0      # 連続発火を防ぐクールダウン
TERM_CLOSE_USE_CLOSEBY = True      # 全決済で close-by を試す（失敗時は成行）
TERM_ALLOW_STEP_DOWN   = False     # 下落でtermを下げない（上方向のみ）
TERM_ROLLOVER_FLOOR    = None      # term達成時の全決済で使うフロア(Noneで無効=ノーフロア)
# ### PATCH: TERM guard option
TERM_RESET_BASE_WHEN_EMPTY = False  # 空ポジ時に_baseを張り直さない（既定 False で安全）
TERM_STATE_FILE           = os.path.join(os.path.dirname(__file__) or ".", "term_state.json")
CLOSE_MIN_PROFIT_FLOOR    = 0.0   # 全決済時にこの利益を下回りそうなら中断して再開
SMART_CLOSE_TOP_WINNERS   = 10    # 全決済時にまとめて捌く利益ポジ本数
# ── Smart Close Settings ──────────────────────────────────────────
SMART_POOL_ENABLE         = True    # Pool蓄積利益を使う (Use Pool Cumulative Profit)
SMART_POOL_INITIAL_USD    = 0.0     # Pool初期値 ($)
SMART_PROFIT_USAGE_RATE   = 50.0   # 決済利益のPool蓄積割合 (%)
SMART_OFFSET_ENABLE       = True    # オフセット決済を有効化
SMART_EQUAL_COUNT_ENABLE  = True    # 同数決済を有効化
SMART_TARGET_CLOSE_ENABLE = True    # 目標決済を有効化
SMART_TARGET_MODE         = "Spread"  # "Spread" (スプレッドベース) or "Balance" (固定USD)
SMART_TARGET_PARAMETER    = 2.0     # Target Close パラメータ (Spread倍率 or 固定USD)
SMART_TARGET_DYNAMIC_MODE_ENABLE = True  # True: mode別のSpread倍率を使う
SMART_TARGET_PARAMETER_RECOVERY = 5.0    # NANPIN(Recovery)
SMART_TARGET_PARAMETER_HOLD     = 6.0    # HOLD
SMART_TARGET_PARAMETER_PYRAMID  = 7.0    # PYRAMID(Trend/Preserve)
SMART_TARGET_MAGIC_NUMBERS = str(MAGIC_NUMBER)  # Smart Close対象Magic (comma-separated)
SMART_CAUTION_MODE        = "strict"  # "strict" | "extended"
SMART_SPREAD_WINDOW_SEC   = 60.0      # SmartCloser parity: 60 sec
SMART_SC_DEVIATION        = 20        # SmartCloser parity deviation for SC closes
SMART_TARGET_AUTO_SCALE_CENT = True   # セント口座を自動検知してTargetを補正
SMART_TARGET_CENT_MULTIPLIER = 100.0  # セント口座時のSpreadTarget補正倍率
SMART_CLOSEBY_WAIT_SEC    = 2.0       # Target close Phase1 wait before market fallback

SC_CLOSE_NONE = 0
SC_CLOSE_CLOSEBY = 1
SC_CLOSE_GATE = 2
SC_CLOSE_DIFF = 3
LIMIT_ENTRY_ENABLE         = False   # 成行エントリーの代わりに指値を優先する
LIMIT_ENTRY_OFFSET_PTS     = 1.0    # 指値を現在値からどれだけ有利に置くか（pts）
LIMIT_ENTRY_MAX_SPREAD_PTS = 500    # スプレッドがこのpts以下なら指値を許可
PIVOT_FAST_TF           = "M1"
PIVOT_MID_TF            = "M5"
PIVOT_SLOW_TF           = "M15"
PIVOT_ULTRA_TF          = None   # 15s等を使うなら "S15"
PIVOT_FLIP_WINDOW_SEC   = 55.0  # M15フリップ後にCDを短縮する時間
PIVOT_FLIP_CD_FACTOR    = 0.5    # フリップ直後のCD倍率（0.5=半分）


# ═════════════════════ I18N（日本語化） ═════════════════════
LANG = "ja"
I18N = {
    "ui.app_title":            "Anya Bot",
    "ui.header.loading":       "口座と対象通貨ペアのデータを読み込み中…",
    "ui.status.starting":      "開始中…",
    "ui.btn.abort_all":        "全決済/指値削除して終了",
    "ui.btn.cancel_only":      "指値削除のみで終了",
    "ui.btn.pause":            "一時停止",
    "ui.btn.resume":           "稼働再開",
    "ui.opt.preserve_protect": "全決済時も利益ポジを温存",
    "ui.monitor.title":        "モニター",
    "ui.monitor.spread":       "スプレッド (pts):",
    "ui.monitor.step":         "ステップ (pts):",
    "ui.monitor.tfl":          "TFL稼働数 (買/売):",
    "ui.monitor.mae":          "MAE稼働数 (買/売):",
    "ui.monitor.pend_b":       "未約定 買い(件/ロット):",
    "ui.monitor.pend_s":       "売り(件/ロット):",
    "ui.monitor.pend_tot":     "合計(件/ロット):",
    "ui.monitor.pos_b":        "ポジション買い(件/ロット/PnL):",
    "ui.monitor.pos_s":        "ポジション売り(件/ロット/PnL):",
    "ui.monitor.pos_net":      "合計(ロット/PnL):",
    "ui.monitor.winside":      "勝ち側PnL / エクスポージャ:",
    "st.tflow.fire":           "TFL実行: {side} ロット={vol}",
    "st.tflow.skip.risk":      "TFLをスキップ（リスクガード: {why}）",
    "log.guard.buy":           "[GUARD] 買いエントリーをスキップ @{price:.2f}: 近すぎ (最小 {min:.5f})",
    "log.guard.sell":          "[GUARD] 売りエントリーをスキップ @{price:.2f}: 近すぎ (最小 {min:.5f})",
    "log.fill.unsupported":    "Filling {mode} 非対応: retcode={code} → リトライ…",
    "log.fill.switched":       "Filling を {mode} に切替（retcode={code}）",
    "log.fill.failed":         "すべてのフィリングモードで失敗: action={action}",
    "log.tflow.no_new":        "TFL: 発注後に新規ポジションを検出できませんでした",
    "log.tflow.err":           "TFLエラー: {err}",
    # v10 Dashboard
    "ui.monitor.basic_title":  "[1] 基本統計 (口座/損益)",
    "ui.monitor.bal_eq":       "残高/余剰証拠金:",
    "ui.monitor.prog":         "目標進捗:",
    "ui.monitor.pos_bsn":      "ポジ数 (買/売/ネ):",
    "ui.monitor.context_title": "[2] 市場環境 & AI分析",
    "ui.monitor.ai_predict":   "AI予測:",
    "ui.monitor.pivot":        "ピボット:",
    "ui.monitor.v10_title":    "[3] v10管理 & 制限状況",
    "ui.monitor.turnover_b":   "回転率 (買い):",
    "ui.monitor.turnover_s":   "回転率 (売り):",
    "ui.monitor.v10_flags":    "v10 特記事項:",
    "ui.monitor.budget_status": "予算状況 (B/S/C/P):",
    "ui.monitor.spread_step":  "スプ/ステップ:",
    "ui.monitor.last_abort":   "直近の中断:",
}
def _t(key: str, **kw) -> str:
    s = I18N.get(key, key)
    try:
        return s.format(**kw) if kw else s
    except Exception:
        return s


# ── P&L thresholds（相殺ロジック用） ─────────────────────────
TOTAL_PROFIT_THRESHOLD = 0.1   # 合計利益がこれ以上なら全クローズ
PAIR_PROFIT_THRESHOLD  = 0.5   # 組合せ純益がこれ以上なら相殺クローズ
OFFSET_ENABLED_ABOVE_PNL = -200 # 総損益がこれより大きれば(軽微なら)同方向相殺を試行
OFFSET_WITH_NEW_ENTRIES = False # 不足分を逆方向の成行で埋める（安全のため既定OFF）

# ── tp-control tuning ────────────────────────────────────────
TP_CTRL_COOLDOWN_SEC = 3.0   # TP調整の実行間隔（過剰注文防止）
TP_MAX_LOSERS = 0            # ★ 最悪N本だけにTPを付ける（0でTP付与なし）
# ── grid step tuning ─────────────────────────────────────────
SPREAD_MULT_DEFAULT = 2

# ── module toggles (UI defaults) ───────────────
GRID_ENABLE            = False  # グリッド自体の起動トグル

# ── trend-follow assist (TFlow) ─────────────────────────────
TFLOW_ENABLE            = False   # 既定OFF（まずは動作確認してからON）
TFLOW_TF                = mt5.TIMEFRAME_M5
TFLOW_EMA_FAST          = 12
TFLOW_EMA_SLOW          = 48
TFLOW_DONCH_N           = 21      # Donchian窓
TFLOW_ATR_PERIOD        = 14
TFLOW_BREAK_ATR_K       = 0.04    # HHV(55) + ATR*k を上抜き/下抜き
TFLOW_MIN_ATR_SPREAD    = 1.3     # ATR/Spread がこれ以上
TFLOW_COOLDOWN_SEC      = 12.0
# ── grid-side selection / hysteresis ─────────────────────────
GRID_MODE_DEFAULT        = "winside_pnl"   # both | winside_pnl | minority_exposure
GRID_WINSIDE_STRICT      = True      # True=負け側pendingを整理
KEEP_NEAREST_SLOTS       = 0          # strict時、負け側に残す近傍本数（総数）
EXPOSURE_FLIP_RATIO      = 0.01       # 少数側へ切替える最小比率（|buy-sell|/max(buy,sell)）
WINSIDE_FLIP_COOLDOWN_SEC= 5.0      # 切替のクールダウン
PNL_FLIP_ABS_USD         = 0.0        # PnL基準での最小差分（0=無効）
TFLOW_MAX_LIVE          = 4       # 片側の同時保有上限
TFLOW_LOT_MULT          = 1     # 基本lotの倍率
TFLOW_SL_MODE           = "none"  # 'none' | 'step'
TFLOW_SL_STEP_MULT      = 1.0


# 併用の順張りアシスト（EMA×step基準）
MAE_ENABLE              = False
MAE_BREAK_K             = 0.35    # ← 0.8 → 0.35（リセンタmid±step*0.35の小ブレイクも拾う）
MAE_MIN_ATR_SPREAD      = 1.25
MAE_COOLDOWN_SEC        = 20.0
MAE_MAX_LIVE            = 1
MAE_LOT_MULT            = 1

# ログを少し詳しく
LOG_VERBOSITY           = 1

# === Allowlist 認証（公開CSV）=========================================
# ★ここをあなたの値に
SHEET_ID    = "2PACX-1vQmUK04jXd4apNsbhrXf70tUTnwOEH1wThDyUpIugq7vmrtULKuSjK2zSd6HBWtAcZ4RPDzGDAjggFx"
SHEET_NAME  = "Allowlist"
ALLOWLIST_URL = r"https://docs.google.com/spreadsheets/d/e/2PACX-1vQmUK04jXd4apNsbhrXf70tUTnwOEH1wThDyUpIugq7vmrtULKuSjK2zSd6HBWtAcZ4RPDzGDAjggFx/pub?gid=155132181&single=true&output=csv"

# オフライン用キャッシュ（任意）
ALLOWLIST_CACHE = os.path.join(os.path.expanduser("~"), ".anya_allow_cache.json")
FAIL_CLOSED = True   # 取得失敗時は停止(True) / キャッシュ不在でも続行(False)

   # 初期値: スプレッドの何倍をグリッド幅に使うか
# === Discord通知設定 ===
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1427833797239378021/2lyh8amRmDHb4VklsI5sI4Cge1FKcFs4LoHWAOHonbiR1fEPI_EDJgor_Oab3lWl2vtz"  # ←あなたのWebhook URLを入れてください

def send_notify(msg: str, title: str = "GridTrader", color: int = 0x00ff00):
    """Discordに通知を送信"""
    try:
        payload = {
            "username": "GridTrader v9",
            "embeds": [
                {
                    "title": title,
                    "description": msg,
                    "color": color
                }
            ]
        }
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        print(f"[NOTIFY ERROR] {e}")




# ═════════════════════════ GUI HELPERS ═════════════════════════
def _discover_terminals() -> list:
    paths = []
    if psutil:
        for p in psutil.process_iter(attrs=["name", "exe"]):
            if "terminal64.exe" in (p.info.get("name") or "").lower():
                exe = p.info.get("exe") or ""
                if exe and exe not in paths:
                    paths.append(exe)
    return paths


def choose_terminal() -> str | None:
    root = tk.Tk(); root.withdraw()
    win = tk.Toplevel(root); win.title("Select MT5 terminal"); win.grab_set()

    cols = ("exe", "login", "server", "balance", "currency", "name")
    tree = ttk.Treeview(win, columns=cols, show="headings", height=8)
    for c, w in zip(cols, (340, 80, 170, 100, 70, 150)):
        tree.heading(c, text=c); tree.column(c, width=w, anchor="w")

    for exe in _discover_terminals():
        mt5.initialize(path=exe)
        acc, _ = mt5.account_info(), mt5.terminal_info()
        if acc:
            tree.insert("", tk.END, values=(exe, acc.login, acc.server,
                                            f"{acc.balance:.2f}", acc.currency, acc.name))
        mt5.shutdown()

    tree.grid(row=0, column=0, columnspan=2, padx=6, pady=6)
    sel: dict = {"path": None}

    def _use() -> None:
        if tree.selection():
            sel["path"] = tree.item(tree.selection()[0], "values")[0]
            win.destroy()

    ttk.Button(win, text="Use", command=_use).grid(row=1, column=1, pady=(0, 6), padx=6, sticky="e")
    if tree.get_children():
        tree.selection_set(tree.get_children()[0])
    win.wait_window(); root.destroy()
    return sel["path"]

def _adjdir_from_series(highs, lows, opens, closes, new_count=True):
    n = len(closes)
    # ── FIX: 参照バー数を増やしたため、ガードを緩める（あるいは5のまま） ──
    if n < 5:
        return 0, None, None, "—"

    range_high = float(highs[0])
    range_low  = float(lows[0])
    last_event = "—"
    base_dir   = 0

    for i in range(1, n):
        ph, pl = float(highs[i]), float(lows[i])
        po, pc = float(opens[i]), float(closes[i])
        
        # 保存用（比較に使う）
        old_rh = range_high
        old_rl = range_low

        lower_low   = pl < old_rl
        higher_high = ph > old_rh

        # インサイドバーなら更新なし
        if not (lower_low or higher_high):
            # Range Inherit: Update range to current bar (Tighten)
            range_high, range_low = ph, pl
            continue

        # 片側ブレイク（通常）: レンジを「その足」にリセット
        # Pine: valid_lower_low -> range_high:=prev_high(ph), range_low:=prev_low(pl)
        if lower_low and not higher_high:
            last_event, base_dir = "下げ止まり", -1
            range_high, range_low = ph, pl
            continue

        if higher_high and not lower_low:
            last_event, base_dir = "上げ止まり", +1
            range_high, range_low = ph, pl
            continue

        # 両抜け (Outside Bar)
        if new_count:
            if pc > po:  # 陽線
                if pc > old_rh:
                    # 高値ブレイク成功 (Pine: trend_type_flag:=2)
                    last_event, base_dir = "上げ止まり", +1
                    range_high = ph
                    range_low  = old_rl # 維持
                elif pc < old_rh:
                    # 高値更新したが戻された -> 下落扱い (Pine: trend_type_flag:=1)
                    last_event, base_dir = "下げ止まり", -1
                    range_low  = pl
                    range_high = old_rh # 維持
                else: # 同値
                    last_event, base_dir = "両抜け", 0
                    range_high, range_low = ph, pl
            elif pc < po:  # 陰線
                if pc < old_rl:
                    # 安値ブレイク成功 (Pine: trend_type_flag:=1)
                    last_event, base_dir = "下げ止まり", -1
                    range_low  = pl
                    range_high = old_rh # 維持
                elif pc > old_rl:
                    # 安値更新したが戻された -> 上昇扱い (Pine: trend_type_flag:=2)
                    last_event, base_dir = "上げ止まり", +1
                    range_high = ph
                    range_low  = old_rl # 維持
                else: # 同値
                    last_event, base_dir = "両抜け", 0
            continue
        else:
            last_event, base_dir = "両抜け", 0
            range_high, range_low = ph, pl

    return int(base_dir), range_high, range_low, last_event


def _adjdir_from_series_with_pivots(highs, lows, opens, closes, new_count=True):
    """
    _adjdir_from_series の拡張版。スイングピボット(前回高値/前回安値)も返す。
    zigzag が方向転換するたびに、前区間の極値を確定スイングピボットとして記録。
    Returns: (base_dir, range_high, range_low, last_event, swing_high, swing_low)
    """
    n = len(closes)
    if n < 5:
        return 0, None, None, "—", None, None

    range_high = float(highs[0])
    range_low  = float(lows[0])
    last_event = "—"
    base_dir   = 0
    swing_high = None
    swing_low  = None
    seg_max_high = float(highs[0])
    seg_min_low  = float(lows[0])

    for i in range(1, n):
        ph, pl = float(highs[i]), float(lows[i])
        po, pc = float(opens[i]), float(closes[i])
        old_rh, old_rl = range_high, range_low
        old_dir = base_dir

        lower_low   = pl < old_rl
        higher_high = ph > old_rh

        if not (lower_low or higher_high):
            range_high, range_low = ph, pl
            seg_max_high = max(seg_max_high, ph)
            seg_min_low  = min(seg_min_low, pl)
            continue

        if lower_low and not higher_high:
            if old_dir > 0:
                swing_high = seg_max_high
            last_event, base_dir = "下げ止まり", -1
            range_high, range_low = ph, pl
            seg_max_high = ph
            seg_min_low  = pl
            continue

        if higher_high and not lower_low:
            if old_dir < 0:
                swing_low = seg_min_low
            last_event, base_dir = "上げ止まり", +1
            range_high, range_low = ph, pl
            seg_max_high = ph
            seg_min_low  = pl
            continue

        # 両抜け (Outside Bar)
        if new_count:
            new_dir = 0
            if pc > po:  # 陽線
                if pc > old_rh:
                    new_dir = +1; last_event = "上げ止まり"
                    if old_dir < 0: swing_low = seg_min_low
                    range_high = ph; range_low = old_rl
                elif pc < old_rh:
                    new_dir = -1; last_event = "下げ止まり"
                    if old_dir > 0: swing_high = seg_max_high
                    range_low = pl; range_high = old_rh
                else:
                    last_event = "両抜け"; range_high, range_low = ph, pl
            elif pc < po:  # 陰線
                if pc < old_rl:
                    new_dir = -1; last_event = "下げ止まり"
                    if old_dir > 0: swing_high = seg_max_high
                    range_low = pl; range_high = old_rh
                elif pc > old_rl:
                    new_dir = +1; last_event = "上げ止まり"
                    if old_dir < 0: swing_low = seg_min_low
                    range_high = ph; range_low = old_rl
                else:
                    last_event = "両抜け"
            else:
                last_event = "両抜け"; range_high, range_low = ph, pl
            if new_dir != 0:
                base_dir = new_dir
                seg_max_high = ph; seg_min_low = pl
            else:
                seg_max_high = max(seg_max_high, ph)
                seg_min_low  = min(seg_min_low, pl)
            continue
        else:
            last_event, base_dir = "両抜け", 0
            range_high, range_low = ph, pl
            seg_max_high = max(seg_max_high, ph)
            seg_min_low  = min(seg_min_low, pl)

    return int(base_dir), range_high, range_low, last_event, swing_high, swing_low


# ========== Unified Dashboard Class (v10.5) ==========
class UnifiedDashboard(ttk.Frame):
    def __init__(self, parent, traders: list, symbol: str, **kwargs):
        super().__init__(parent, **kwargs)
        self.traders = traders # [t1, t2, t3]
        self.symbol = symbol
        
        # UI Setup
        self._setup_ui()
        # Start Loop
        self._update_loop()

    def _setup_ui(self):
        # Title
        ttk.Label(self, text=f"Unified Dashboard: {self.symbol}", font=("Arial", 14, "bold"))\
            .pack(pady=10)
        
        # Summary Table
        frame_table = ttk.LabelFrame(self, text="Real-time Summary")
        frame_table.pack(padx=10, pady=5, fill="x")
        
        # [FIX] Enhanced Columns: Added "Cum PnL" per user request
        self.tree = ttk.Treeview(frame_table, columns=("Mode", "Pos(B/S)", "Vol(Net)", "PnL", "CumPnL", "Status"), show="headings", height=5)
        self.tree.heading("Mode", text="Mode")
        self.tree.heading("Pos(B/S)", text="Pos (Buy/Sell)")
        self.tree.heading("Vol(Net)", text="Vol (Net)")
        self.tree.heading("PnL", text="PnL (Float)")
        self.tree.heading("CumPnL", text="Cum PnL (Real)")
        self.tree.heading("Status", text="Status (Mode)")
        
        self.tree.column("Mode", width=80, anchor="center")
        self.tree.column("Pos(B/S)", width=100, anchor="center")
        self.tree.column("Vol(Net)", width=80, anchor="center")
        self.tree.column("PnL", width=100, anchor="e")
        self.tree.column("CumPnL", width=100, anchor="e")
        self.tree.column("Status", width=160, anchor="center")
        self.tree.pack(fill="x", padx=5, pady=5)

        # [FIX] Global Control Buttons
        frame_btns = ttk.Frame(self)
        frame_btns.pack(pady=10)
        
        btn_abort = ttk.Button(frame_btns, text="ABORT ALL (Stop & Close)", command=self._abort_all)
        btn_abort.pack(side="left", padx=10)
        
        btn_close = ttk.Button(frame_btns, text="Close ALL (Keep Running)", command=self._close_all)
        btn_close.pack(side="left", padx=10)

        # Account Info (Global)
        frame_acct = ttk.Frame(self)
        frame_acct.pack(pady=10)
        self.lbl_acct = ttk.Label(frame_acct, text="Account: Loading...", font=("Arial", 11))
        self.lbl_acct.pack()

        # Smart Close Status Panel
        frame_sc = ttk.LabelFrame(self, text="Smart Close")
        frame_sc.pack(padx=10, pady=5, fill="x")
        self.lbl_sc = ttk.Label(frame_sc, text="Initializing...", font=("Arial", 10), justify="left")
        self.lbl_sc.pack(padx=5, pady=4, anchor="w")

    def _abort_all(self):
        """Emergency Abort for ALL instances"""
        if not messagebox.askyesno("ABORT ALL", "全てのモードを停止し、全ポジションを決済しますか？\n(各タブのAbortと同様の動作です)"):
            return
        for t in self.traders:
            t._abort() # This is now thread-safe / isolated per instance
            
    def _close_all(self):
        """Close All Positions for ALL instances"""
        if not messagebox.askyesno("CLOSE ALL", "全てのモードのポジションを決済しますか？\n(稼働は継続します)"):
            return
        for t in self.traders:
            t._log("[DASHBOARD] Global Close triggered.", level=1)
            t._full_close()

    def _update_loop(self):
        try:
            # 1. Gather Data
            rows = []
            total_b, total_s = 0, 0
            total_vol, total_pnl, total_cum = 0.0, 0.0, 0.0
            
            for t in self.traders:
                # Access trader's GUI vars safely (they are tk.StringVar)
                # Parse "Pos: 2/0.02/+10.5" type strings? 
                # Better: Access t._last_gui_pos_info if available or calculate fresh
                
                # Check logic: t should have `_get_my_positions` now!
                # But iterating raw positions might be heavy depending on frequency.
                # Let's use the GUI vars which are already updated by the trader thread.
                # format: "B: 2/0.02/+150"
                # Actually _mon_vars["pos_b"] is "2/0.02/+150.00"
                
                try:
                    mode_name = t.profile.name.split(" ")[0] # Scalp, Day, Swing
                except: mode_name = "Unknown"

                # Parse B
                b_str = t._mon_vars["pos_b"].get() # "2/0.02/+150.00"
                parts_b = b_str.split("/")
                b_n = int(parts_b[0]) if len(parts_b)>0 and parts_b[0].isdigit() else 0
                b_v = float(parts_b[1]) if len(parts_b)>1 else 0.0
                b_p = float(parts_b[2]) if len(parts_b)>2 else 0.0
                
                # Parse S
                s_str = t._mon_vars["pos_s"].get()
                parts_s = s_str.split("/")
                s_n = int(parts_s[0]) if len(parts_s)>0 and parts_s[0].isdigit() else 0
                s_v = float(parts_s[1]) if len(parts_s)>1 else 0.0
                s_p = float(parts_s[2]) if len(parts_s)>2 else 0.0
                
                # Net
                net_v = b_v - s_v
                sum_p = b_p + s_p
                
                # Status (Mode Status now)
                status = t._mon_vars["mode_status"].get() # "PYRAMID", "HOLD (55s)"
                
                # [FIX] Fetch Realized PnL (Cumulative for current Term)
                cum_pnl = t._get_my_realized_pnl(t._term_start_ts)
                
                rows.append((mode_name, f"{b_n} / {s_n}", f"{net_v:+.2f}", f"{sum_p:+.2f}", f"{cum_pnl:+.2f}", status))
                
                total_b += b_n; total_s += s_n
                total_vol += net_v; total_pnl += sum_p
                total_cum += cum_pnl
            
            # 2. Update Tree
            # Clear old
            for item in self.tree.get_children():
                self.tree.delete(item)
            
            # Insert Sub-rows
            for r in rows:
                self.tree.insert("", "end", values=r)
            
            # Insert Total
            self.tree.insert("", "end", values=("TOTAL", f"{total_b} / {total_s}", f"{total_vol:+.2f}", f"{total_pnl:+.2f}", f"{total_cum:+.2f}", "—"), tags=("total",))
            self.tree.tag_configure("total", background="#dddddd", font=("Arial", 10, "bold"))

            # 3. Update Account
            # Access any trader's term_cur (Balance/Equity)
            cur_bal = self.traders[0]._mon_vars["term_cur"].get()
            self.lbl_acct.config(text=f"Total {cur_bal}")

            # 4. Smart Close Status (Pool / Fixed / State)
            sc_lines = []
            for t in self.traders:
                try:
                    pool  = getattr(t, "_sc_pool",  0.0)
                    fixed = getattr(t, "_sc_fixed", 0.0)
                    state = getattr(t, "_sc_last_state", "Good Condition")
                    mode  = t.profile.name.split(" ")[0]
                    sc_lines.append(f"[{mode}] Pool:${pool:.2f} Fixed:${fixed:.2f} | {state}")
                except Exception:
                    pass
            if sc_lines and hasattr(self, "lbl_sc"):
                self.lbl_sc.config(text="\n".join(sc_lines))

        except Exception as e:
            pass

        # Schedule next
        self.after(1000, self._update_loop)




# ========== 方向（M1/M15/H1）: 状態保持 + 未確定バーのブレイク反映 ==========
class TFDirection:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.state = {"M1": None, "M15": None, "H1": None}

    # 呼び出し元が bars を指定しない場合は 300 (AI版に合わせる)
    def _dir_one(self, tf_name: str, tf_const: int, bars: int = 300):
        # _debug_log(f"[DIRDBG-{tf_name}] Fetching {bars} bars (const={tf_const})...")
        rates = mt5.copy_rates_from_pos(self.symbol, tf_const, 0, bars)
        if rates is None or len(rates) < 6:
            return 0, None, None, "—"

        his, now = rates[:-1], rates[-1]
        opens  = [r["open"]  for r in his]
        highs  = [r["high"]  for r in his]
        lows   = [r["low"]   for r in his]
        closes = [r["close"] for r in his]
        base_dir, rh, rl, ev = _adjdir_from_series(highs, lows, opens, closes, new_count=True)

        # ... (中略) ...
        
        self.state[tf_name] = st
        return st["dir"], st.get("rh", rh), st.get("rl", rl), display_ev
    
    # 明示的に 300 を指定 (AI版と同じ)
    # 明示的に 300 を指定 (AI版と同じ)
    def m1(self):  return self._dir_one("M1",  mt5.TIMEFRAME_M1, 300)
    def M5(self):  return self._dir_one("M5",  mt5.TIMEFRAME_M5, 300)
    def M15(self): return self._dir_one("M15", mt5.TIMEFRAME_M15, 300)
    def H1(self):  return self._dir_one("H1",  mt5.TIMEFRAME_H1, 300)
    def H4(self):  return self._dir_one("H4",  mt5.TIMEFRAME_H4, 300)
    def D1(self):  return self._dir_one("D1",  mt5.TIMEFRAME_D1, 300)


# ========== Timeframe Profile Structure v10.4 ==========
class TimeframeProfile:
    def __init__(self, name: str, exec_tf, c1_tf, c2_tf, ref1_tf, ref2_tf,
                 cd_sec: float, hold_sec: float, parent_map: dict, profit_th: float | None = None,
                 term_target_profit_usd: float | None = None):
        self.name = name
        self.exec_tf = exec_tf       # 実行足 (TIMEFRAME_M1 etc)
        self.c1_tf = c1_tf           # コンテキスト1 (M5)
        self.c2_tf = c2_tf           # コンテキスト2 (M15)
        self.ref1_tf = ref1_tf       # 上位参照1 (H1)
        self.ref2_tf = ref2_tf       # 上位参照2 (H4)
        self.cd_sec = cd_sec         # エントリー後クールダウン
        self.hold_sec = hold_sec     # 最低保有時間 (55s rule)
        self.parent_map = parent_map # {exec: c1, c1: c2, c2: ref1...}
        self.profit_th = profit_th
        self.term_target_profit_usd = term_target_profit_usd

    @property
    def exec_str(self):
        return self._tf_to_str(self.exec_tf)

    def _tf_to_str(self, tf):
        m = {mt5.TIMEFRAME_M1:"M1", mt5.TIMEFRAME_M5:"M5", mt5.TIMEFRAME_M15:"M15",
             mt5.TIMEFRAME_H1:"H1", mt5.TIMEFRAME_H4:"H4", mt5.TIMEFRAME_D1:"D1", mt5.TIMEFRAME_W1:"W1"}
        return m.get(tf, "M1")

        return m.get(tf, "M1")


# ── Persistence Helpers ──
CONFIG_KEYS = [
    "symbol", "digits", "lot", "side", "spread_mult", "pair_profit_th",
    "mae_cd", "tflow_cd", "grid_enable", "mae_enable", "tflow_enable",
    "grid_mode", "strict", "keep_nearest", "flip_ratio", "flip_cd", "min_dist_mult",
    "tfl_live", "tfl_mult", "mae_live", "mae_mult",
    "pivot_enable", "pivot_cd", "pivot_skip_c1",
    "discord_enable", "discord_url",
    "volatility_enable", "volatility_mult",
    "block_time_enable", "block_time_start", "block_time_end",
    "profile",
    "req_ref2_bb", "req_upper_bb", "zigzag_gate", "nanpin_hedge", "nanpin_prevent"
]

def _get_config_path(symbol: str = "XAUUSD") -> str:
    safe_sym = "".join(c for c in symbol if c.isalnum())
    return os.path.join(os.path.dirname(__file__) or ".", f"anyabot_config_{safe_sym}.json")

def _load_config_from_disk(symbol: str = "XAUUSD") -> dict:
    """全設定を読み込む。無ければ空dict"""
    path = _get_config_path(symbol)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_config_to_disk(symbol: str, config: dict) -> bool:
    """全設定をJSONに保存"""
    path = _get_config_path(symbol)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        _debug_log(f"Failed to save config: {e}")
        return False

def _get_profile_path(symbol):
    safe_sym = "".join(c for c in symbol if c.isalnum())
    return os.path.join(os.path.dirname(__file__) or ".", f"profile_{safe_sym}.json")

def _load_profile_from_disk(symbol):
    # 新config形式を優先
    cfg = _load_config_from_disk(symbol)
    if cfg.get("profile"):
        return cfg["profile"]
    # 旧形式のフォールバック
    path = _get_profile_path(symbol)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("profile", "Scalp (M1)")
        except: pass
    return "Scalp (M1)"


class ParamDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk):
        super().__init__(parent)
        self.title("初期設定"); self.grab_set()
        self.res: tuple | None = None

        # Load saved config for default symbol
        self._saved_cfg = _load_config_from_disk(DEF_SYMBOL)
        
        # Load initial profile (from saved config or default)
        init_prof = self._saved_cfg.get("profile") or _load_profile_from_disk(DEF_SYMBOL)
        self.var_profile = tk.StringVar(value=init_prof)
        
        # Profile Selection
        ttk.Label(self, text="初期プロファイル").grid(row=0, column=2, padx=(12,4), pady=4, sticky="e")
        prof_cb = ttk.Combobox(self, textvariable=self.var_profile, values=["Scalp (M1)", "Day (M5)", "Swing (M15)", "Unified (M1+M5+M15)", "Smart (M5+M15)"], state="readonly", width=12)
        prof_cb.grid(row=0, column=3, padx=(0,12), pady=4, sticky="w")

        # Helper to get saved value with fallback
        def _cfg(key, default):
            v = self._saved_cfg.get(key)
            return str(v) if v is not None else default

        rows = (
            ("通貨ペア",            _cfg("symbol", DEF_SYMBOL)),
            ("価格の桁数",      _cfg("digits", str(DEF_DIGITS))),
            ("基準ロット",          _cfg("lot", f"{DEF_LOT:.2f}")),
            ("グリッドの数",     _cfg("side", str(DEF_ORDERS_SIDE))),
            ("スプの何倍をグリッド幅にするか",  _cfg("spread_mult", f"{SPREAD_MULT_DEFAULT:.2f}")),
            # ▼ 追加：相殺しきい値（USD）
            ("相殺時の利益", _cfg("pair_profit_th", f"{PAIR_PROFIT_THRESHOLD:.2f}")),
            ("MAEクールダウン(秒)", _cfg("mae_cd", f"{MAE_COOLDOWN_SEC:.0f}")),
            ("TFlowクールダウン(秒)", _cfg("tflow_cd", f"{TFLOW_COOLDOWN_SEC:.0f}")),
            ("TFL同時本数(片側)", _cfg("tfl_live", f"{TFLOW_MAX_LIVE:d}")),
            ("TFLロット倍率", _cfg("tfl_mult", f"{TFLOW_LOT_MULT:.2f}")),
            ("MAE同時本数(片側)", _cfg("mae_live", f"{MAE_MAX_LIVE:d}")),
            ("MAEロット倍率", _cfg("mae_mult", f"{MAE_LOT_MULT:.2f}")),
        )


        self.vars: list[tk.StringVar] = []
        for r, (label, default) in enumerate(rows):
            ttk.Label(self, text=label).grid(row=r, column=0, sticky="w", padx=6, pady=4)
            var = tk.StringVar(value=default); self.vars.append(var)
            ttk.Entry(self, textvariable=var, width=18).grid(row=r, column=1, sticky="w", padx=6, pady=4)

        # ★追加：ATR/Spread から倍率を自動計算して代入するボタン
        ttk.Button(self, text="Auto (ATR/Spread)", command=self._auto_mult)\
            .grid(row=4, column=2, padx=6, pady=4, sticky="w")

        # ── modules toggles (load from saved config) ──
        self.var_grid = tk.BooleanVar(value=self._saved_cfg.get("grid_enable", GRID_ENABLE))
        self.var_mae = tk.BooleanVar(value=self._saved_cfg.get("mae_enable", MAE_ENABLE))
        self.var_tflow = tk.BooleanVar(value=self._saved_cfg.get("tflow_enable", TFLOW_ENABLE))
        self.var_grid_mode = tk.StringVar(value=self._saved_cfg.get("grid_mode", 'minority_exposure'))
        self.var_strict = tk.BooleanVar(value=self._saved_cfg.get("strict", True))
        self.var_keep_nearest = tk.StringVar(value=str(self._saved_cfg.get("keep_nearest", '0')))
        self.var_flip_ratio = tk.StringVar(value=str(self._saved_cfg.get("flip_ratio", '0.12')))
        self.var_flip_cd = tk.StringVar(value='10')
        self.var_pivot_skip_c1 = tk.BooleanVar(value=PIVOT_STRICT_SKIP_C1)
        frm = ttk.LabelFrame(self, text="ロジック")
        frm.grid(row=len(rows)+1, column=0, columnspan=4, padx=6, pady=(6,0), sticky="w")
        ttk.Checkbutton(frm, text="グリッド", variable=self.var_grid).grid(row=0, column=0, padx=(8,12), pady=6, sticky="w")
        ttk.Checkbutton(frm, text="AI(MAE)",  variable=self.var_mae).grid(row=0, column=1, padx=(0,12), pady=6, sticky="w")
        ttk.Checkbutton(frm, text="AI(TFL)",variable=self.var_tflow).grid(row=0, column=2, padx=(0,12), pady=6, sticky="w")
        # ▼ Pivot一致エントリー (M15×M5、h1一致で2本)
        self.var_pivot    = tk.BooleanVar(value=True)   # 既定ON（必要ならFalse）
        self.var_pivot_cd = tk.StringVar(value="60")

        ttk.Checkbutton(frm, text="Anya", variable=self.var_pivot).grid(row=0, column=3, padx=(8,12), pady=6, sticky="w")
        ttk.Label(frm, text="Pivotクールダウン(秒)").grid(row=5, column=1, padx=(0,4), sticky="e")
        ttk.Entry(frm, width=8, textvariable=self.var_pivot_cd).grid(row=5, column=2, padx=(0,12), sticky="w")
        ttk.Checkbutton(frm, text="Pivot Skip C1 (2TF)", variable=self.var_pivot_skip_c1).grid(row=5, column=3, padx=(0,12), sticky="w")

        # ── v10.6: 4新オプション ──
        self.var_req_ref2_bb = tk.BooleanVar(value=self._saved_cfg.get("req_ref2_bb", PIVOT_FIRST_REQUIRE_REF2_BODY_BREAK))
        self.var_req_upper_bb = tk.BooleanVar(value=self._saved_cfg.get("req_upper_bb", PIVOT_FIRST_REQUIRE_UPPER_BODY_BREAK))
        self.var_zigzag_gate = tk.BooleanVar(value=self._saved_cfg.get("zigzag_gate", PIVOT_ZIGZAG_ENTRY_ENABLE))
        self.var_nanpin_hedge = tk.BooleanVar(value=self._saved_cfg.get("nanpin_hedge", NANPIN_FULL_HEDGE_ENABLE))
        self.var_nanpin_prevent = tk.BooleanVar(value=self._saved_cfg.get("nanpin_prevent", NANPIN_PREVENT_ENABLE))
        ttk.Checkbutton(frm, text="環境認識BB", variable=self.var_req_ref2_bb).grid(row=6, column=0, padx=(8,12), pady=2, sticky="w")
        ttk.Checkbutton(frm, text="監視足BB", variable=self.var_req_upper_bb).grid(row=6, column=1, padx=(0,12), pady=2, sticky="w")
        ttk.Checkbutton(frm, text="ZZ Gate", variable=self.var_zigzag_gate).grid(row=6, column=2, padx=(0,12), pady=2, sticky="w")
        ttk.Checkbutton(frm, text="Nanpin Hedge", variable=self.var_nanpin_hedge).grid(row=6, column=3, padx=(0,12), pady=2, sticky="w")
        ttk.Checkbutton(frm, text="NP Guard", variable=self.var_nanpin_prevent).grid(row=6, column=4, padx=(0,12), pady=2, sticky="w")

        # Grid mode radios
        ttk.Radiobutton(frm, text="両側", value="both", variable=self.var_grid_mode).grid(row=1, column=0, padx=(8,12), pady=2, sticky="w")
        ttk.Radiobutton(frm, text="勝ち側(PnL)", value="pnl", variable=self.var_grid_mode).grid(row=1, column=1, padx=(0,12), pady=2, sticky="w")
        ttk.Radiobutton(frm, text="少数側(Exposed)", value="minority_exposure", variable=self.var_grid_mode).grid(row=1, column=2, padx=(0,12), pady=2, sticky="w")
        ttk.Radiobutton(frm, text="Pivot同期", value="pivot_follow", variable=self.var_grid_mode).grid(row=1, column=3, padx=(0,12), pady=2, sticky="w")
        # Strict / hysteresis controls
        ttk.Checkbutton(frm, text="strict:負け側pending整理", variable=self.var_strict).grid(row=2, column=0, padx=(8,12), pady=2, sticky="w")
        ttk.Label(frm, text="残す近傍N本").grid(row=2, column=1, padx=(0,4), sticky="e")
        ttk.Entry(frm, width=6, textvariable=self.var_keep_nearest).grid(row=2, column=2, padx=(0,12), sticky="w")
        ttk.Label(frm, text="flip閾値(比率)").grid(row=3, column=0, padx=(8,4), sticky="e")
        ttk.Entry(frm, width=8, textvariable=self.var_flip_ratio).grid(row=3, column=1, padx=(0,12), sticky="w")
        ttk.Label(frm, text="flipクールダウン(秒)").grid(row=3, column=2, padx=(0,4), sticky="e")
        ttk.Entry(frm, width=8, textvariable=self.var_flip_cd).grid(row=3, column=3, padx=(0,12), sticky="w")

        # ▼ 最小距離ガード(×step)
        ttk.Label(frm, text="最小距離(×step)").grid(row=4, column=0, padx=(8,4), sticky="e")
        self.var_min_dist = tk.StringVar(value=f"{MIN_ENTRY_DISTANCE_MULT:.2f}")
        ttk.Entry(frm, width=8, textvariable=self.var_min_dist).grid(row=4, column=1, padx=(0,12), sticky="w")
        
        # ── Notifications Frame ──
        # Load config
        self.discord_conf_path = os.path.join(os.path.dirname(__file__) or ".", "discord_config.json")
        saved_url = ""
        saved_en = True
        try:
            if os.path.exists(self.discord_conf_path):
                with open(self.discord_conf_path, "r", encoding="utf-8") as f:
                    conf = json.load(f)
                    saved_url = conf.get("url", "")
                    saved_en = bool(conf.get("enable", True))
        except: pass

        nf = ttk.LabelFrame(self, text="通知 (Notification)")
        nf.grid(row=len(rows)+2, column=0, columnspan=4, padx=6, pady=(6,0), sticky="ew")
        
        self.var_discord_en = tk.BooleanVar(value=saved_en)
        self.var_discord_url = tk.StringVar(value=saved_url)
        
        ttk.Checkbutton(nf, text="Discord通知", variable=self.var_discord_en).grid(row=0, column=0, padx=(8,4), pady=6, sticky="w")
        ttk.Label(nf, text="Webhook URL:").grid(row=0, column=1, padx=(4,2), sticky="e")
        ttk.Entry(nf, textvariable=self.var_discord_url, width=40).grid(row=0, column=2, padx=(2,12), sticky="w")
        
        # ── Risk Guard Frame ──
        rf = ttk.LabelFrame(self, text="リスクガード (Risk Guard)")
        rf.grid(row=len(rows)+3, column=0, columnspan=4, padx=6, pady=(6,0), sticky="ew")
        
        # ATR Volatility Guard
        self.var_volatility_en = tk.BooleanVar(value=VOLATILITY_GUARD_ENABLE)
        self.var_volatility_mult = tk.StringVar(value=f"{VOLATILITY_ATR_MULT:.1f}")
        ttk.Checkbutton(rf, text="ボラティリティガード", variable=self.var_volatility_en).grid(row=0, column=0, padx=(8,4), pady=6, sticky="w")
        ttk.Label(rf, text="ATR倍率:").grid(row=0, column=1, padx=(4,2), sticky="e")
        ttk.Entry(rf, textvariable=self.var_volatility_mult, width=6).grid(row=0, column=2, padx=(2,12), sticky="w")
        
        # Time Block
        self.var_block_time_en = tk.BooleanVar(value=BLOCK_TIME_ENABLE)
        self.var_block_time_start = tk.StringVar(value=str(BLOCK_TIME_START_HOUR))
        self.var_block_time_end = tk.StringVar(value=str(BLOCK_TIME_END_HOUR))
        ttk.Checkbutton(rf, text="時間帯ブロック", variable=self.var_block_time_en).grid(row=1, column=0, padx=(8,4), pady=6, sticky="w")
        ttk.Label(rf, text="開始時:").grid(row=1, column=1, padx=(4,2), sticky="e")
        ttk.Entry(rf, textvariable=self.var_block_time_start, width=4).grid(row=1, column=2, padx=(2,4), sticky="w")
        ttk.Label(rf, text="終了時:").grid(row=1, column=3, padx=(4,2), sticky="e")
        ttk.Entry(rf, textvariable=self.var_block_time_end, width=4).grid(row=1, column=4, padx=(2,12), sticky="w")
        
        ttk.Button(self, text="開始", command=self._ok).grid(row=len(rows)+4, column=1, pady=8, sticky="e")

    
    def _ok(self):
        """入力を検証してダイアログの戻り値に格納→閉じる"""
        try:
            sym   = self.vars[0].get().strip()
            digs  = int(self.vars[1].get())
            lot   = float(self.vars[2].get())
            nside = int(self.vars[3].get())
            smult = float(self.vars[4].get())
            pair_th = float(self.vars[5].get())  # ▼追加

            if not sym:
                raise ValueError("Symbol is empty.")
            if smult < 1.0:
                smult = 1.0  # 極端に狭いグリッドを防止

            mae_cd = float(self.vars[6].get())
            tf_cd = float(self.vars[7].get())
            tfl_live = int(self.vars[8].get())
            tfl_mult = float(self.vars[9].get())
            mae_live = int(self.vars[10].get())
            mae_mult = float(self.vars[11].get())

            grid_mode = self.var_grid_mode.get()
            if grid_mode == 'pnl':
                grid_mode = 'winside_pnl'
            strict = bool(self.var_strict.get())
            keepn = int(self.var_keep_nearest.get() or '0')
            flipr = float(self.var_flip_ratio.get() or '0.15')
            flipcd= float(self.var_flip_cd.get() or '120')
            min_dist_mult = float(self.var_min_dist.get() or '0.6')

            # Risk Guard settings
            volatility_en = bool(self.var_volatility_en.get())
            volatility_mult = float(self.var_volatility_mult.get() or "2.0")
            block_time_en = bool(self.var_block_time_en.get())
            block_time_start = int(self.var_block_time_start.get())
            block_time_end = int(self.var_block_time_end.get())
            
            # Profile
            prof_name = self.var_profile.get()

            self.res = (
                sym, digs, lot, nside, smult, pair_th,
                mae_cd, tf_cd, 
                bool(self.var_grid.get()), bool(self.var_mae.get()), bool(self.var_tflow.get()),
                grid_mode, strict, keepn, flipr, flipcd, min_dist_mult,
                tfl_live, tfl_mult, mae_live, mae_mult,
                bool(self.var_pivot.get()), float(self.var_pivot_cd.get() or "60"), bool(self.var_pivot_skip_c1.get()),
                bool(self.var_discord_en.get()), self.var_discord_url.get().strip(),
                volatility_en, volatility_mult, block_time_en, block_time_start, block_time_end,
                prof_name,
                bool(self.var_req_ref2_bb.get()), bool(self.var_req_upper_bb.get()),
                bool(self.var_zigzag_gate.get()), bool(self.var_nanpin_hedge.get()),
                bool(self.var_nanpin_prevent.get())
            )

            # ★ Save all config to JSON
            _save_config_to_disk(sym, {
                "symbol": sym, "digits": digs, "lot": lot, "side": nside,
                "spread_mult": smult, "pair_profit_th": pair_th,
                "mae_cd": mae_cd, "tflow_cd": tf_cd,
                "grid_enable": bool(self.var_grid.get()),
                "mae_enable": bool(self.var_mae.get()),
                "tflow_enable": bool(self.var_tflow.get()),
                "grid_mode": grid_mode, "strict": strict,
                "keep_nearest": keepn, "flip_ratio": flipr, "flip_cd": flipcd,
                "min_dist_mult": min_dist_mult,
                "tfl_live": tfl_live, "tfl_mult": tfl_mult,
                "mae_live": mae_live, "mae_mult": mae_mult,
                "pivot_enable": bool(self.var_pivot.get()),
                "pivot_cd": float(self.var_pivot_cd.get() or "60"),
                "pivot_skip_c1": bool(self.var_pivot_skip_c1.get()),
                "discord_enable": bool(self.var_discord_en.get()),
                "discord_url": self.var_discord_url.get().strip(),
                "volatility_enable": volatility_en, "volatility_mult": volatility_mult,
                "block_time_enable": block_time_en,
                "block_time_start": block_time_start, "block_time_end": block_time_end,
                "profile": prof_name,
                "req_ref2_bb": bool(self.var_req_ref2_bb.get()),
                "req_upper_bb": bool(self.var_req_upper_bb.get()),
                "zigzag_gate": bool(self.var_zigzag_gate.get()),
                "nanpin_hedge": bool(self.var_nanpin_hedge.get()),
                "nanpin_prevent": bool(self.var_nanpin_prevent.get())
            })
            
            self.destroy()
        except ValueError as e:
            import tkinter.messagebox
            tkinter.messagebox.showerror("Error", f"Invalid input: {e}")
            block_time_start = int(self.var_block_time_start.get() or "0")
            block_time_end = int(self.var_block_time_end.get() or "1")
            
            self.res = (
                sym, digs, lot, nside, smult, pair_th,
                mae_cd, tf_cd,
                bool(self.var_grid.get()), bool(self.var_mae.get()), bool(self.var_tflow.get()),
                grid_mode, strict, keepn, flipr, flipcd, min_dist_mult,
                tfl_live, tfl_mult, mae_live, mae_mult,bool(self.var_pivot.get()), float(self.var_pivot_cd.get() or "10"), bool(self.var_pivot_skip_c1.get()),
                bool(self.var_discord_en.get()), str(self.var_discord_url.get()).strip(),
                volatility_en, volatility_mult, block_time_en, block_time_start, block_time_end
            )

            # Save Discord Config
            try:
                with open(self.discord_conf_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "enable": bool(self.var_discord_en.get()),
                        "url": str(self.var_discord_url.get()).strip()
                    }, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"Failed to save discord config: {e}")

            self.destroy()
        except Exception as e:
            from tkinter import messagebox
            messagebox.showerror("Input error", str(e))

    def _auto_mult(self):
        """M5のATR(5)と現行スプレッドから推奨倍率を計算してSpread mult欄へ代入"""
        try:
            sym = self.vars[0].get().strip()
            if not sym:
                raise ValueError("Symbol is empty.")
            info = mt5.symbol_info(sym)
            tick = mt5.symbol_info_tick(sym)
            if not info or not tick:
                raise ValueError("Symbol info/tick not available. MT5接続やシンボル選択を確認してください。")
            pt = info.point or 0.0
            if pt <= 0.0:
                raise ValueError("Invalid point size.")

            # スプレッド（pts）
            spread_pts = int(round((tick.ask - tick.bid) / pt)) if (tick and pt) else int(info.spread or 0)
            if spread_pts <= 0:
                raise ValueError("Spread is zero.")

            # M5の直近6本（先頭は比較用）から ATR(5) を計算
            rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M5, 1, 6)
            if rates is None or len(rates) < 6:
                raise ValueError("Not enough M5 bars.")

            prev_close = rates[0]['close']
            trs = []
            for r in rates[1:]:
                high, low, close = r['high'], r['low'], r['close']
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                trs.append(tr)
                prev_close = close
            atr = sum(trs[-5:]) / 5.0
            atr_pts = atr / pt

            # 推奨倍率 ≒ clamp( 0.5 * (ATR_pts / spread_pts), 2, 12 )
            R = atr_pts / spread_pts
            mult = max(2.0, min(12.0, 1.0 * R))

            self.vars[4].set(f"{mult:.2f}")
            messagebox.showinfo("Auto", f"推奨倍率 {mult:.2f} をセットしました（ATR1M15/spread = {R:.2f}）")
        except Exception as e:
            messagebox.showerror("Auto 計算エラー", str(e))



# ═════════════════════════ Allowlist 関連 ════════════════════════════
def fetch_allowlist_csv() -> set[int]:
    """公開CSVから許可アカウント集合を取得。失敗時はキャッシュを利用。"""
    try:
        r = requests.get(ALLOWLIST_URL, timeout=5)
        r.raise_for_status()
        rows = list(csv.reader(r.text.splitlines()))
        ids = set()
        for row in rows:
            if not row: continue
            cell = (row[0] or "").strip()
            if not cell: continue
            if cell.isdigit():
                ids.add(int(cell))
        # キャッシュ保存
        try:
            with open(ALLOWLIST_CACHE, "w", encoding="utf-8") as f:
                json.dump({"ids": sorted(ids), "ts": time.time()}, f, ensure_ascii=False)
        except Exception:
            pass
        return ids
    except Exception as e:
        # キャッシュ復旧
        try:
            with open(ALLOWLIST_CACHE, "r", encoding="utf-8") as f:
                obj = json.load(f)
            return set(int(x) for x in obj.get("ids", []))
        except Exception:
            if FAIL_CLOSED:
                raise RuntimeError(f"認証リストが見つかりませんでした: {e}")
            return set()  # 続行モード

# ═════════════════════════ OFFSET TRANSACTION ══════════════════════
class OffsetTransaction:
    """
    相殺トランザクションの状態管理。
    Boss（最悪ポジ）が決済されるまで、消費したWinner数を累積する。
    """
    def __init__(self, boss_ticket: int, boss_side: int, winner_side: int):
        self.boss_ticket = boss_ticket          # Target loser position ticket
        self.boss_side = boss_side              # mt5.POSITION_TYPE_BUY or SELL
        self.winner_side = winner_side          # Opposite side
        self.winners_closed = 0                 # Accumulated count of closed winners
        self.winners_closed_vol = 0.0           # Accumulated volume (for lot calc)
        self.started_at = time.time()           # When transaction began
        self.last_activity = time.time()        # Last successful close
        self.status = "active"                  # "active", "paused", "complete", "cancelled"
    
    def add_winner(self, vol: float = 0.01):
        """Winner決済時に呼び出す"""
        self.winners_closed += 1
        self.winners_closed_vol += vol
        self.last_activity = time.time()
    
    def complete(self):
        self.status = "complete"
    
    def cancel(self):
        self.status = "cancelled"

# ═════════════════════════ TRADER CLASS ════════════════════════
class StopGridTrader:
    def __init__(self,
        terminal_path: str | None = None,
        symbol: str = DEF_SYMBOL,
        digits: int = DEF_DIGITS,
        base_lot: float = DEF_LOT,
        orders_side: int = DEF_ORDERS_SIDE,
        headless: bool = False,
        # module toggles
        grid_enable: bool = GRID_ENABLE,
        mae_enable: bool = MAE_ENABLE,
        grid_winside_only: bool = False,
        grid_mode: str = GRID_MODE_DEFAULT,
        strict_pending_cleanup: bool = GRID_WINSIDE_STRICT,
        keep_nearest_slots: int = KEEP_NEAREST_SLOTS,
        exposure_flip_ratio: float = EXPOSURE_FLIP_RATIO,
        winside_flip_cooldown_sec: float = WINSIDE_FLIP_COOLDOWN_SEC,
        min_entry_distance_mult: float = MIN_ENTRY_DISTANCE_MULT,
        pnl_flip_abs_usd: float = PNL_FLIP_ABS_USD,

        # cooldowns
        recenter_cooldown: float = 5.0,
        pairnet_cooldown: float = PAIRNET_COOLDOWN_SEC,
        # pair-net
        pairnet_enable: bool = PAIRNET_ENABLE,
        pairnet_min_net_profit: float = PAIRNET_MIN_NET_PROFIT,
        pairnet_max_pos: int = PAIRNET_MAX_POS_TO_USE,
        pairnet_arm_ratio: float = PAIRNET_ARM_RATIO,
        pairnet_disarm_ratio: float = PAIRNET_DISARM_RATIO,
        use_close_by: bool = USE_CLOSE_BY,
        # logging
        log_file: str = LOG_FILE,
        # guards
        max_positions: int = MAX_POSITIONS,
        max_total_positions: int = MAX_TOTAL_POSITIONS,
        max_volume: float = MAX_VOLUME,
        max_pending: int = MAX_PENDING,
        max_orders_per_min: int = MAX_ORDERS_PER_MIN,
        max_recenter_per_min: int = MAX_RECENTER_PER_MIN,
        dd_stop_pct: float = DD_STOP_PCT,
        spread_max_pts: int = SPREAD_MAX_PTS,
        hard_stop_equity: float | None = HARD_STOP_EQUITY,
        # step auto-tune
        step_mode: str = STEP_MODE_DEFAULT,
        recompute_step_on_recenter: bool = RECOMPUTE_ON_RECENTER,
        spread_cap_pts: int = SPREAD_CAP_PTS,
        step_pts_min_user: int = STEP_PTS_MIN_USER,
        step_pts_max_user: int = STEP_PTS_MAX_USER,
        step_price_pct: float = STEP_PRICE_PCT,
        step_abs_usd: float | None = STEP_ABS_USD,
        fixed_step_pts: int | None = FIXED_STEP_PTS,
        min_tp_spread_mult: float = MIN_TP_SPREAD_MULT,
        # slot
        slot_enable: bool = SLOT_ENABLE,
        slot_width_mult: float = SLOT_WIDTH_MULT,
        slot_cleanup_per_recenter: int = SLOT_CLEANUP_PER_RECENTER,
        # stuck-aware
        stuck_enable: bool = STUCK_ENABLE,
        stuck_min_losers: int = STUCK_MIN_LOSERS,
        stuck_adverse_mult: float = STUCK_ADVERSE_MULT,
        stuck_arm_max_winners: int = STUCK_ARM_MAX_WINNERS,
        spread_mult: float = SPREAD_MULT_DEFAULT,
        pair_profit_threshold: float = PAIR_PROFIT_THRESHOLD,
        stuck_arm_cooldown: float = STUCK_ARM_COOLDOWN_SEC,
        tflow_enable: bool = TFLOW_ENABLE,
        tflow_tf: int = TFLOW_TF,
        tflow_ema_fast: int = TFLOW_EMA_FAST,
        tflow_ema_slow: int = TFLOW_EMA_SLOW,
        tflow_donch_n: int = TFLOW_DONCH_N,
        tflow_atr_period: int = TFLOW_ATR_PERIOD,
        tflow_break_atr_k: float = TFLOW_BREAK_ATR_K,
        tflow_min_atr_spread: float = TFLOW_MIN_ATR_SPREAD,
        tflow_cooldown: float = TFLOW_COOLDOWN_SEC,
        tflow_max_live: int = TFLOW_MAX_LIVE,
        tflow_lot_mult: float = TFLOW_LOT_MULT,
        tflow_sl_mode: str = TFLOW_SL_MODE,
        tflow_sl_step_mult: float = TFLOW_SL_STEP_MULT,
        mae_max_live: int = MAE_MAX_LIVE,
        mae_lot_mult: float = MAE_LOT_MULT,
        mae_cooldown:float = MAE_COOLDOWN_SEC,
        pivot_enable: bool = True,
        pivot_cooldown_sec: float = 119.0, # Changed from 60.0
        pivot_block_after_offset_sec: float = 5.0,
        pivot_strict_skip_c1: bool = PIVOT_STRICT_SKIP_C1,
        pivot_skip_c1_override: bool | None = None,
        pivot_first_require_ref2_body_break: bool = PIVOT_FIRST_REQUIRE_REF2_BODY_BREAK,
        pivot_first_require_upper_body_break: bool = PIVOT_FIRST_REQUIRE_UPPER_BODY_BREAK,
        pivot_zigzag_entry_enable: bool = PIVOT_ZIGZAG_ENTRY_ENABLE,
        pivot_first_entry_relax_enable: bool = PIVOT_FIRST_ENTRY_RELAX_ENABLE,
        pivot_first_body_break_only_enable: bool = PIVOT_FIRST_BODY_BREAK_ONLY_ENABLE,
        m1_pairnet_enable: bool = M1_PAIRNET_ENABLE,
        nanpin_full_hedge_enable: bool = NANPIN_FULL_HEDGE_ENABLE,
        nanpin_prevent_enable: bool = NANPIN_PREVENT_ENABLE,
        close_min_profit_floor: float = CLOSE_MIN_PROFIT_FLOOR,
        limit_entry_enable: bool = LIMIT_ENTRY_ENABLE,
        limit_entry_offset_pts: float = LIMIT_ENTRY_OFFSET_PTS,
        limit_entry_max_spread_pts: float = LIMIT_ENTRY_MAX_SPREAD_PTS,
        term_state_file: str = TERM_STATE_FILE,
        term_rollover_floor: float | None = TERM_ROLLOVER_FLOOR,
        pivot_fast_tf: str = PIVOT_FAST_TF,
        pivot_mid_tf: str = PIVOT_MID_TF,
        pivot_slow_tf: str = PIVOT_SLOW_TF,
        pivot_ultra_tf: str | None = PIVOT_ULTRA_TF,
        pivot_flip_window_sec: float = PIVOT_FLIP_WINDOW_SEC,
        pivot_flip_cd_factor: float = PIVOT_FLIP_CD_FACTOR,
        # ### PATCH: term args
        term_enable: bool = TERM_ENABLE,
        term_use_equity: bool = TERM_USE_EQUITY,
        term_step_usd: float = TERM_STEP_USD,
        term_min_hold_sec: float = TERM_MIN_HOLD_SEC,
        term_cooldown_sec: float = TERM_COOLDOWN_SEC,
        term_close_use_closeby: bool = TERM_CLOSE_USE_CLOSEBY,
        term_allow_step_down: bool = TERM_ALLOW_STEP_DOWN,
        # __init__(...) の引数に追加
        pivot_hot_window_sec: float = 10.0,   # 何秒以内の決済ならHot扱いか
        pivot_hot_cd_factor: float = 0.02,    # Hot時のCD倍率（30%に短縮）
        pivot_cold_cd_factor: float = 1,   # Cold時のCD倍率（1.5倍に延長）
        pivot_hot_override: bool = False,      # Hot時はCDをバイパス（=即時）するか
        # Discord Info
        discord_enable: bool = True,
        discord_url: str = "",
        # Risk Guard settings
        volatility_guard_enable: bool = VOLATILITY_GUARD_ENABLE,
        volatility_atr_mult: float = VOLATILITY_ATR_MULT,
        block_time_enable: bool = BLOCK_TIME_ENABLE,
        block_time_start_hour: int = BLOCK_TIME_START_HOUR,
        block_time_end_hour: int = BLOCK_TIME_END_HOUR,
        initial_profile_name: str = "Scalp (M1)",
        magic: int = MAGIC_NUMBER,
        smart_close_top_winners: int = SMART_CLOSE_TOP_WINNERS,
        gui_parent: tk.Widget | None = None,
        # Smart Close
        smart_pool_enable: bool = SMART_POOL_ENABLE,
        smart_pool_initial_usd: float = SMART_POOL_INITIAL_USD,
        smart_profit_usage_rate: float = SMART_PROFIT_USAGE_RATE,
        smart_offset_enable: bool = SMART_OFFSET_ENABLE,
        smart_equal_count_enable: bool = SMART_EQUAL_COUNT_ENABLE,
        smart_target_close_enable: bool = SMART_TARGET_CLOSE_ENABLE,
        smart_target_mode: str = SMART_TARGET_MODE,
        smart_target_parameter: float = SMART_TARGET_PARAMETER,
        smart_target_magic_numbers: str = SMART_TARGET_MAGIC_NUMBERS,
        smart_caution_mode: str = SMART_CAUTION_MODE,
        smart_spread_window_sec: float = SMART_SPREAD_WINDOW_SEC,
        smart_deviation: int = SMART_SC_DEVIATION,
    ):
        super().__init__()
        
        self.path   = terminal_path
        self.symbol = symbol
        self.digits = digits
        self.magic  = magic
        self.gui_parent = gui_parent
        # v10.4 Persistence
        self._initial_profile_name = initial_profile_name
        self.pivot_ultra_tf = pivot_ultra_tf
        self.lot    = base_lot
        self.side   = orders_side
        self.mae_cooldown = float(mae_cooldown) 
        self._nanpin_lock = False  # Global Nanpin Mode Lock
        self.smart_close_top_winners = int(smart_close_top_winners)

        # [NEW] Smart Profile State
        self._smart_profile_enable = False
        self._smart_base_profile = None  # "Day (M5)" or "Swing (M15)" when Active
        self._smart_state = "STANDBY"    # "STANDBY" | "ACTIVE_M5" | "ACTIVE_M15" | "NANPIN"
        self._current_mode_str = "IDLE"  # Current mode status: "IDLE", "NANPIN (Recovery)", "PYRAMID (Preserve)", etc.
        self._entry_family_bar_state: dict[tuple[str, str], int] = {}

        self.mid: float | None = None
        self.step_pts: int | None = None
        self.running = False

        self.last_recent_ts = 0.0
        self.recenter_cooldown = recenter_cooldown

        # module toggles
        self.grid_enable = bool(grid_enable)
        self._pivot_state: dict[str, dict] = {}

        # range filters per TF (enable/disable)
        self.pivot_range_filter_M15 = getattr(self, 'pivot_range_filter_M15', True)
        self.pivot_range_filter_M5  = getattr(self, 'pivot_range_filter_M5',  True)
        self.pivot_range_filter_M1  = getattr(self, 'pivot_range_filter_M1',  True)


        
        self.grid_winside_only = bool(grid_winside_only)
        # grid-side selection
        self.grid_mode = str(grid_mode or GRID_MODE_DEFAULT)
        if self.grid_winside_only and self.grid_mode == "both":
            self.grid_mode = "winside_pnl"  # 後方互換
        self.strict_pending_cleanup = bool(strict_pending_cleanup)
        self.keep_nearest_slots = int(keep_nearest_slots)
        self.exposure_flip_ratio = float(exposure_flip_ratio)
        self.winside_flip_cooldown_sec = float(winside_flip_cooldown_sec)
        self.pnl_flip_abs_usd = float(pnl_flip_abs_usd)
        self._last_grid_side = None  # 'buy' | 'sell' | None
        self._last_grid_flip_ts = 0.0
        self._last_pivot_dir_memory = 0  # Pivot direction: 1=buy, -1=sell, 0=none
# pair-net
        self.pairnet_enable = pairnet_enable
        self.pairnet_cooldown = pairnet_cooldown
        self.pairnet_min_net_profit = pairnet_min_net_profit
        self.pairnet_max_pos = max(2, pairnet_max_pos)
        self.pairnet_arm_ratio = pairnet_arm_ratio
        self.pairnet_disarm_ratio = pairnet_disarm_ratio
        self.use_close_by = use_close_by
        self._offset_retry_queue: list[dict] = []
        self._offset_queue_anchor_side: str | None = None
        self._offset_tx: OffsetTransaction | None = None  # Offset Transaction State
        self._closing_in_progress: bool = False           # エントリー抑止用フラグ（インスタンス限定）
        self._closing_reason: str = ""
        # 総利益クローズ再開用の進行状態
        self._global_close_state: dict[str, float | bool | float] = {
            "active": False,
            "realized_profit": 0.0,
            "started_ts": 0.0,
        }

        # --- Dynamic Paths Localization ---
        safe_sym = "".join(c for c in self.symbol if c.isalnum())
        prof_suffix = self._initial_profile_name.replace(" ", "_").replace("(", "").replace(")", "")
        self.global_close_state_file = os.path.join(os.path.abspath(os.path.dirname(__file__) or "."), f"global_close_state_{safe_sym}_{prof_suffix}.json")
        self._default_log_file = os.path.join(os.path.dirname(__file__) or ".", f"pairnet_log_{safe_sym}_{prof_suffix}.txt")
        self.ai_log_file = os.path.join(os.path.abspath(os.path.dirname(__file__) or "."), f"anyabot_ai_log_{safe_sym}_{prof_suffix}.csv")
        self.state_log_file = os.path.join(os.path.abspath(os.path.dirname(__file__) or "."), f"anyabot_state_log_{safe_sym}_{prof_suffix}.csv")
        self.debug_log_file = os.path.join(os.path.abspath(os.path.dirname(__file__) or "."), f"anyabot_debug_{safe_sym}_{prof_suffix}.log")
        self.pivot_config_file = os.path.join(os.path.abspath(os.path.dirname(__file__) or "."), f"pivot_config_{safe_sym}.json")
        self.term_state_file = term_state_file # keep as provided or localize? usually provided.
        # override global if not headless
        _debug_log(f"[INIT] {self._initial_profile_name} magic={self.magic}")
        
        # ★v10.2: Offset状態永続化（中断/再開時の余計な決済防止）
        self._offset_state = {
            "active": False,                 # 相殺進行中フラグ
            "closed_tickets": set(),         # 閉じたticket（再決済防止）
            "realized_profit": 0.0,          # 確定利益累計
            "winner_count_buy": 0,           # BUY側で閉じたWinner数
            "winner_count_sell": 0,          # SELL側で閉じたWinner数
            "last_boss_side": None,          # 最後のBoss方向
        }
        
        
        # ★v10.3: 初期状態ロード (Persistence)
        self._last_offset_complete_ts = time.time() # default
        self._bot_start_ts = time.time()  # ★v10.3: 起動猶予用タイムスタンプ
        self._load_offset_state_from_disk()
        self._load_global_close_state()
        if pivot_skip_c1_override is None:
            self._load_pivot_config()
        else:
            self.pivot_strict_skip_c1 = bool(pivot_skip_c1_override)
        self._save_pivot_config()

        # ── ML gate state ────────────────────────────────────
        self.ai_enable = bool(getattr(self, "ai_enable", AI_ENABLE))
        self.ai_threshold = float(getattr(self, "ai_threshold", AI_THRESHOLD))
        self.ai_model_path = str(getattr(self, "ai_model_path", AI_MODEL_PATH))
        self.ai_log_enable = bool(getattr(self, "ai_log_enable", AI_LOG_ENABLE))
        self.ai_log_file = str(getattr(self, "ai_log_file", AI_LOG_FILE))
        self.ai_debug = bool(getattr(self, "ai_debug", AI_DEBUG))
        self.ai_model = None
        self.ai_active = False
        self._ai_cache_key = None
        self._ai_cache_feat = None
        self._session_id = uuid.uuid4().hex[:8]
        self._entry_hist_buy: list[float] = []
        self._entry_hist_sell: list[float] = []
        self._close_hist_buy: list[float] = []
        self._close_hist_sell: list[float] = []
        self._last_entry_budget_log_ts = 0.0
        self.state_log_enable = bool(getattr(self, "state_log_enable", STATE_LOG_ENABLE))
        self.state_log_file = str(getattr(self, "state_log_file", STATE_LOG_FILE))
        self.state_log_interval_sec = float(getattr(self, "state_log_interval_sec", STATE_LOG_INTERVAL_SEC))
        self._last_state_log_ts = 0.0
        # Entry budget UI cache
        self._budget_status_cache = {"buy": "B:—/—", "sell": "S:—/—"}
        # Per-Iteration Cache (populated at top of _monitor loop)
        self._iter_tick = None
        self._iter_info = None
        self._iter_positions = None
        self._iter_orders = None
        # GUI thread should read snapshots only (avoid direct MT5 calls from tkinter thread).
        self._last_positions_snapshot = []
        self._last_tick_time = 0
        self._last_tick_time_sec = 0
        self._stale_tick_start: float | None = None
        self._market_closed_stale = False
        self._monitor_hb_ts = 0.0
        # Per-Bar Cache for _tf_dir results: {tf_str: (bar_open_time, result_tuple)}
        self._tf_dir_cache = {}

        self._last_tp_ctrl_ts = 0.0
        self.spread_mult = float(spread_mult)
        self.min_entry_distance_mult = float(globals().get('MIN_ENTRY_DISTANCE_MULT', 0.6))
        # ▼ 追加：相殺しきい値
        self.pair_profit_threshold = float(pair_profit_threshold)
        self.tflow_enable = tflow_enable
        self.tflow_tf = tflow_tf
        self.tflow_ema_fast = tflow_ema_fast
        self.tflow_ema_slow = tflow_ema_slow
        self.tflow_donch_n = tflow_donch_n
        self.tflow_atr_period = tflow_atr_period
        self.tflow_break_atr_k = float(tflow_break_atr_k)
        self.tflow_min_atr_spread = float(tflow_min_atr_spread)
        self.tflow_cooldown = float(tflow_cooldown)
        self.tflow_max_live = int(tflow_max_live)
        self.tflow_lot_mult = float(tflow_lot_mult)
        self.tflow_sl_mode = (tflow_sl_mode or "none").lower()
        self.tflow_sl_step_mult = float(tflow_sl_step_mult)
        self._last_tflow_ts = 0.0

        self.mae_enable = bool(mae_enable)
        self.mae_break_k = 0.35
        self.mae_cooldown = float(mae_cooldown)
        self.mae_max_live = int(mae_max_live)
        self.mae_lot_mult = float(mae_lot_mult)
        self.mae_min_atr_spread = 1.25
        # --- last-entry memory by origin (for min-distance guard) ---
        self._last_entry_by_origin = {
            "grid":    {"buy": None, "sell": None},
            "tflow":   {"buy": None, "sell": None},
            "mae":     {"buy": None, "sell": None},
            "offset":  {"buy": None, "sell": None},
            # ▼ pivot を独立バケットで管理
            "pivot1h": {"buy": None, "sell": None},
            "pivot5m": {"buy": None, "sell": None},
            "pivot1m": {"buy": None, "sell": None},
        }

        # alias: recenter は grid と同一バケットで判定
        self._origin_alias = {"recenter": "grid"}
        # Pivot一致エントリーの既定と状態
        self.pivot_enable = bool(pivot_enable)
        self.pivot_strict_skip_c1 = bool(pivot_strict_skip_c1)  # 2TF一致モード（C1スキップ）
        self.pivot_first_require_ref2_body_break = bool(pivot_first_require_ref2_body_break)
        self.pivot_first_require_upper_body_break = bool(pivot_first_require_upper_body_break)
        self.pivot_zigzag_entry_enable = bool(pivot_zigzag_entry_enable)
        self.pivot_first_entry_relax_enable = bool(getattr(self, "pivot_first_entry_relax_enable", pivot_first_entry_relax_enable))
        self.pivot_first_body_break_only_enable = bool(getattr(self, "pivot_first_body_break_only_enable", pivot_first_body_break_only_enable))
        self.m1_pairnet_enable = bool(getattr(self, "m1_pairnet_enable", m1_pairnet_enable))
        self.nanpin_full_hedge_enable = bool(nanpin_full_hedge_enable)
        self.nanpin_prevent_enable = bool(nanpin_prevent_enable)
        # Zigzag state
        self._zz_entry_flag = False
        self._zz_c2_clear = False
        self._zz_swing_high = None
        self._zz_swing_low = None
        # Nanpin hedge state (net cap + M1確定方式)
        self._nanpin_hedge_done = False      # 少なくとも1回ヘッジ済みか
        self._nanpin_hedge_vol = 0.0         # 現在のヘッジ済みロット数
        self._nanpin_hedge_last_ts = 0.0     # 最終ヘッジ発注時刻 (cooldown用)
        self.pivot_cooldown_sec = float(pivot_cooldown_sec)
        self.pivot_block_after_offset_sec = float(pivot_block_after_offset_sec)
        self.close_min_profit_floor = float(close_min_profit_floor)
        self.limit_entry_enable = bool(limit_entry_enable)
        self.limit_entry_offset_pts = float(limit_entry_offset_pts)
        self.limit_entry_max_spread_pts = float(limit_entry_max_spread_pts)
        # Persistence files
        self.term_state_file = term_state_file
        self.budget_state_file = os.path.join(os.path.dirname(__file__) or ".", f"budget_state_{self.symbol}.json")

        self.term_rollover_floor = term_rollover_floor
        
        # Internal Trading Clock (Frozen during weekend/off-hours)
        self._internal_trading_time = 0.0
        self._last_tick_real_ts = time.time()
        self._last_budget_save_ts = 0.0
        
        # Budget history lists
        self._entry_hist_buy = []
        self._entry_hist_sell = []
        self._close_hist_buy = []
        self._close_hist_sell = []
        
        self._load_budget_history()
        self.pivot_fast_tf = pivot_fast_tf
        self.pivot_mid_tf = pivot_mid_tf
        self.pivot_slow_tf = pivot_slow_tf
        self.pivot_ultra_tf = pivot_ultra_tf
        self.pivot_flip_window_sec = float(pivot_flip_window_sec)
        self.pivot_flip_cd_factor = float(pivot_flip_cd_factor)
        self.trading_paused: bool = False  # 一時停止フラグ
        self.preserve_on_ladder_var = None # Tkinter用 (setup_guiで初期化)
        self._last_pivot_ts: float = 0.0
        self._last_M5_dir: int | None = None
        self._last_M15_dir: int | None = None
        # 監視UI用（直近本数）
        self._last_pivot_entries_n: int = 0
        self._last_pivot_entries_ts: float = 0.0
        self._last_offset_block_ts: float = 0.0
        # --- Pivot(H1/5m/1m) 独立トリガ用の直近方向/発火時刻 ---
        self._last_h1_dir:  int | None = None
        self._last_M15_dir:  int | None = None
        self._last_M5_fast: int | None = None  # 1m

        self._last_pivot1h_ts: float = 0.0
        self._last_step_recalc_ts = 0.0
        self._last_close_log = 0.0
        self._pivot_logic_state = {}  # GUI表示用のロジック状態 (Sync/Override情報など)
        
        # ── init components ──tings/state
        self.term_enable = bool(term_enable)
        self.term_use_equity = bool(term_use_equity)
        self.term_step_usd = float(term_step_usd)
        self.term_min_hold_sec = float(term_min_hold_sec)
        self.term_cooldown_sec = float(term_cooldown_sec)
        self.term_close_use_closeby = bool(term_close_use_closeby)
        self.term_allow_step_down = bool(term_allow_step_down)

        self._term_base: float | None = None
        self._term_last_roll_ts: float = 0.0
        self._term_start_ts: float = time.time()
        self._term_init_balance: float | None = None
        self._term_init_equity: float | None = None
        self._term_daemon_started: bool = False
        self._term_last_bal_snap: float | None = None
        self._term_last_eq_snap: float | None = None
        # __init__ 内のプロパティ初期化（既存の近くに追記）
        self._last_close_ts: float = 0.0
        self._last_close_ts: float = 0.0
        self._last_close_event_ts: float = 0.0  # UI/動的CD共通で参照するタイムスタンプ
        
        
        # Discord
        self.discord_url = discord_url
        self.discord_enable_init = discord_enable

        # Risk Guard
        self.volatility_guard_enable = bool(volatility_guard_enable)
        self.volatility_atr_mult = float(volatility_atr_mult)
        self.block_time_enable = bool(block_time_enable)
        self.block_time_start_hour = int(block_time_start_hour)
        self.block_time_end_hour = int(block_time_end_hour)

        # ── anti-chop state ───────────────────────────────────────────
        self._chop_until_ts: float = 0.0
        self._flip_hist: dict[str, list[float]] = {"M15": [], "M5": [], "H1": []}
        self._last_chop_log_ts: float = 0.0

        # ── Smart Close Settings ──────────────────────────────────────
        self.smart_pool_enable        = bool(smart_pool_enable)
        self.smart_profit_usage_rate  = float(smart_profit_usage_rate)
        self.smart_offset_enable      = bool(smart_offset_enable)
        self.smart_equal_count_enable = bool(smart_equal_count_enable)
        self.smart_target_close_enable= bool(smart_target_close_enable)
        self.smart_target_mode        = str(smart_target_mode)
        self.smart_target_parameter   = float(smart_target_parameter)
        self.smart_target_magic_numbers = str(smart_target_magic_numbers)
        self.smart_caution_mode = str(smart_caution_mode or SMART_CAUTION_MODE).strip().lower()
        if self.smart_caution_mode not in ("strict", "extended"):
            self.smart_caution_mode = SMART_CAUTION_MODE
        self.smart_sc_deviation = int(smart_deviation)
        # ── Smart Close State ─────────────────────────────────────────
        self._sc_pool: float = float(smart_pool_initial_usd)  # Pool残高 ($)
        self._sc_fixed: float = 0.0                            # Fixed (確定利益) 残高 ($)
        # RESTRICT_NONE=0, RESTRICT_ONE_SIDE_BUY=1, RESTRICT_ONE_SIDE_SELL=2, RESTRICT_BOTH=3
        self._sc_restriction: int = 0
        self._sc_last_state: str = "Good Condition"
        # スプレッド履歴 (spread_pts, timestamp) リスト
        self._sc_spread_history: list = []
        self._sc_spread_window_sec: float = float(smart_spread_window_sec)
        self._sc_target_magics: set[int] = set()
        self._sc_comm_cache: dict[int, float] = {}
        self._sc_close_active: bool = False
        self._sc_close_phase: int = SC_CLOSE_NONE
        self._sc_orig_buy_count: int = 0
        self._sc_orig_sell_count: int = 0
        self._sc_manual_close: bool = False
        self._sc_close_start_ts: float = 0.0
        self._sc_target_tickets: set[int] = set()
        self._sc_close_by_supported: bool = False
        self._sc_closeby_phase_start_ts: float = 0.0
        self._sc_closeby_pairs_requested: int = 0
        self._sc_target_scale_cache: float | None = None
        self._sc_last_scale_log_ts: float = 0.0
        self._sc_boot_log_done: bool = False
        self._sc_parse_target_magics()






        # ── Spread Auto Tuning State ──
        self.spread_max_pts = int(spread_max_pts) # Explicit init to avoid AttributeError
        self.spread_auto_mode = bool(SPREAD_FILTER_AUTO)
        self._spread_ema: float | None = None
        self._spread_max_dynamic: float = float(self.spread_max_pts)
        self._offset_enable_flag: bool = True
        self._preserve_toggle_flag: bool = bool(PROFIT_PRESERVE_ENABLE)
        self._discord_enable_flag: bool = bool(discord_enable)

        # gui/log
        self._ui_queue: queue.Queue | None = None
        self.headless = headless
        self._ignore_magic_flag: bool = False
        # If caller did not override, use profile-suffixed default
        if log_file == LOG_FILE or log_file is None:
            log_file = self._default_log_file
        self.log_file = log_file
        print(f"[INIT] Log file will be saved to: {os.path.abspath(self.log_file)}")
        if not self.headless:
            self.root = self.gui_parent if self.gui_parent else tk.Tk()
            self._ui_queue = queue.Queue()
            if not self.gui_parent:
                self.root.title(_t("ui.app_title"))
            
            self.status = tk.StringVar(value=_t("ui.status.starting"))
            # ▼ 追加：口座番号＆シンボルのヘッダー
            self.header = tk.StringVar(value=_t("ui.header.loading"))
            
            # Use self.root for grid placements
            ttk.Label(self.root, textvariable=self.header, font=("Segoe UI", 10, "bold")).grid(padx=12, pady=(10, 0))
            ttk.Label(self.root, textvariable=self.status).grid(padx=12, pady=10)
            ttk.Button(self.root, text=_t("ui.btn.abort_all"), command=self._abort).grid(pady=(0, 10))
            # 追加：未約定だけキャンセルして終了（現在の self.symbol 限定）
            ttk.Button(self.root, text=_t("ui.btn.cancel_only"),command=self._cancel_and_exit).grid(pady=(0, 10))

            # ▼ Monitor frame
            self._mon_vars = {
                "spread": tk.StringVar(value="-"),
                "step": tk.StringVar(value="-"),
                "tfl_live": tk.StringVar(value="-"),
                "mae_live": tk.StringVar(value="-"),
                "limits": tk.StringVar(value=f"TFL live≤{self.tflow_max_live}, MAE live≤{self.mae_max_live}"),
                "pend_b": tk.StringVar(value='-'),
                "pend_s": tk.StringVar(value='-'),
                "pend_tot": tk.StringVar(value='-'),
                "pos_b": tk.StringVar(value='-'),
                "pos_s": tk.StringVar(value='-'),
                "pos_net": tk.StringVar(value='-'),
                "winside_pnl": tk.StringVar(value='-'),
                "winside_expo": tk.StringVar(value='-'),
                "pivot_last": tk.StringVar(value='0'),   # 直近のPivot本数
                # ── TF方向モニタ（+1/0/-1 を矢印テキスト化して表示）
                # ── TF方向モニタ (Generic 5 Slots)
                "dir_1":  tk.StringVar(value="—"), # Exec
                "dir_2":  tk.StringVar(value="—"), # C1
                "dir_3":  tk.StringVar(value="—"), # C2
                "dir_4":  tk.StringVar(value="—"), # Ref1
                "dir_5":  tk.StringVar(value="—"), # Ref2
                "lbl_tf1": tk.StringVar(value="M1:"),
                "lbl_tf2": tk.StringVar(value="M5:"),
                "lbl_tf3": tk.StringVar(value="M15:"),
                "lbl_tf4": tk.StringVar(value="H1:"),
                "lbl_tf5": tk.StringVar(value="H4:"),
                "term_base": tk.StringVar(value='-'),
                "term_target": tk.StringVar(value='-'),
                "term_cur": tk.StringVar(value='-'),
                "term_prog": tk.StringVar(value='-'),
                "mode_status": tk.StringVar(value="IDLE"),
                "bal_start": tk.StringVar(value='-'),
                # Smart Close
                "sc_state": tk.StringVar(value="Good Condition"),
                "sc_pool":  tk.StringVar(value="$0.00"),
                "sc_fixed": tk.StringVar(value="$0.00"),
                "sc_target": tk.StringVar(value="—"),
                # v10 Dashboard
                "turnover_b":       tk.StringVar(value="0.00"),
                "turnover_s":       tk.StringVar(value="0.00"),
                "v10_flags":        tk.StringVar(value="—"),
                "last_abort":       tk.StringVar(value="None"),
                # 既存
                "pivot_heat":       tk.StringVar(value='—'),
                "pivot_cd":         tk.StringVar(value='—'),
                "last_close_ago":   tk.StringVar(value='—'),
                "pivot_flags":      tk.StringVar(value='—'),
                "ai_status":        tk.StringVar(value="AI: —"),
                "ai_prob":          tk.StringVar(value="p: —"),
                "entry_budget":     tk.StringVar(value="budget: —"),
                # リスクガード状態表示
                "entry_budget":     tk.StringVar(value="budget: —"),
                # リスクガード状態表示
                "volatility_status": tk.StringVar(value="—"),
                
                # [NEW] Ignore Magic Option (Persistent)
                "ignore_magic":     tk.BooleanVar(value=False),
                "time_block_status": tk.StringVar(value="—"),
                # ★v10.1: Offset/Pairnet表示
                "offset_mode": tk.StringVar(value="—"),
                "offset_state": tk.StringVar(value="—"),  # ★v10.2: 相殺中断状態
                "m1_pairnet_last": tk.StringVar(value="—"),
                
                # [NEW] Runtime Grid Width Control
                "spread_mult_rt": tk.StringVar(value=f"{spread_mult:.2f}"),
            }
            # Internal mirror for thread-safe reads
            self._ignore_magic_flag = bool(self._mon_vars["ignore_magic"].get())
            def _on_ignore_magic_change(*_):
                try:
                    self._ignore_magic_flag = bool(self._mon_vars["ignore_magic"].get())
                except Exception:
                    pass
            self._mon_vars["ignore_magic"].trace_add("write", _on_ignore_magic_change)
            # v10.4: Timeframe Profiles (Moved up for GUI availability)
            self.profiles = {
                "Scalp (M1)": TimeframeProfile("Scalp (M1)", mt5.TIMEFRAME_M1, mt5.TIMEFRAME_M5, mt5.TIMEFRAME_M15, mt5.TIMEFRAME_H1, mt5.TIMEFRAME_H4, 
                                               55.0, 55.0, {"M1": "M5", "M5": "M15", "M15": "H1", "H1": "H4"}, profit_th=None, term_target_profit_usd=70.0),
                "Day (M5)":   TimeframeProfile("Day (M5)",   mt5.TIMEFRAME_M5, mt5.TIMEFRAME_M15, mt5.TIMEFRAME_H1, mt5.TIMEFRAME_H4, mt5.TIMEFRAME_D1, 
                                               295.0, 295.0, {"M5": "M15", "M15": "H1", "H1": "H4", "H4": "D1"}, profit_th=10.0, term_target_profit_usd=140.0),
                "Swing (M15)":TimeframeProfile("Swing (M15)",mt5.TIMEFRAME_M15, mt5.TIMEFRAME_H1, mt5.TIMEFRAME_H4, mt5.TIMEFRAME_D1, mt5.TIMEFRAME_W1, 
                                               895.0, 895.0, {"M15": "H1", "H1": "H4", "H4": "D1", "D1": "W1"}, profit_th=10.0, term_target_profit_usd=240.0),
            }
            # Load initial profile
            p_name = getattr(self, "_initial_profile_name", "Scalp (M1)")
            smart_boot = (p_name == "Smart (M5+M15)")
            if p_name not in self.profiles and not smart_boot:
                p_name = "Scalp (M1)"
            
            # Smart起動時はDay(M5)を監視ベースにしつつSmartフラグを立てる
            if smart_boot:
                self._smart_profile_enable = True
                self._smart_state = "STANDBY"
                self._smart_base_profile = None
                p_name = "Day (M5)"
            
            self.profile = self.profiles[p_name]
            self.profile_var = tk.StringVar(value="Smart (M5+M15)" if smart_boot else self.profile.name) # Ensure initialized before GUI usage

            # [FIX] Apply Profile Params (CD / Hold) on Init
            if hasattr(self.profile, "cd_sec"):
                self.pivot_cooldown_sec = float(self.profile.cd_sec)
            if hasattr(self.profile, "hold_sec"):
                self.term_min_hold_sec = float(self.profile.hold_sec)
            # [FIX] Apply Profile Profit Threshold
            if hasattr(self.profile, "profit_th") and self.profile.profit_th is not None:
                self.pair_profit_threshold = float(self.profile.profit_th)
            if hasattr(self.profile, "term_target_profit_usd") and self.profile.term_target_profit_usd is not None:
                self.term_target_profit_usd = float(self.profile.term_target_profit_usd)
            
            if smart_boot:
                self._set_status("Smart: Standby (Day)")

            # [FIX] Pre-Initialize Profile TF List for Background Thread Safety
            self._profile_tf_list = [
                self.profile.exec_tf,
                self.profile.c1_tf,
                self.profile.c2_tf,
                self.profile.ref1_tf,
                self.profile.ref2_tf
            ]

        # ━━━ DashBoard Layout ━━━
            # 1. Basic Stats (Account / Positions)
            mon_basic = ttk.LabelFrame(self.root, text=_t("ui.monitor.basic_title"))
            mon_basic.grid(padx=12, pady=(0,5), sticky="ew")

            ttk.Label(mon_basic, text=_t("ui.monitor.bal_eq")).grid(row=0, column=0, sticky="e", padx=4, pady=2)
            ttk.Label(mon_basic, textvariable=self._mon_vars["term_cur"]).grid(row=0, column=1, sticky="w", padx=4, pady=2)
            ttk.Label(mon_basic, text=_t("ui.monitor.prog")).grid(row=0, column=2, sticky="e", padx=4, pady=2)
            ttk.Label(mon_basic, textvariable=self._mon_vars["term_prog"]).grid(row=0, column=3, sticky="w", padx=4, pady=2)

            ttk.Label(mon_basic, text=_t("ui.monitor.pos_bsn")).grid(row=1, column=0, sticky="e", padx=4, pady=2)
            ttk.Label(mon_basic, textvariable=self._mon_vars["pos_b"]).grid(row=1, column=1, sticky="w", padx=4, pady=2)
            ttk.Label(mon_basic, textvariable=self._mon_vars["pos_s"]).grid(row=1, column=2, sticky="w", padx=4, pady=2)
            ttk.Label(mon_basic, textvariable=self._mon_vars["pos_net"]).grid(row=1, column=3, sticky="w", padx=4, pady=2)

            # 2. Context & AI (Market Analysis)
            mon_context = ttk.LabelFrame(self.root, text=_t("ui.monitor.context_title"))
            mon_context.grid(padx=12, pady=(0,5), sticky="ew")

            # TF Directions (Dynamic Labels) 5 Slots
            self._mon_vars["lbl_tf1"] = tk.StringVar(value="TF1:")
            self._mon_vars["lbl_tf2"] = tk.StringVar(value="TF2:")
            self._mon_vars["lbl_tf3"] = tk.StringVar(value="TF3:")
            self._mon_vars["lbl_tf4"] = tk.StringVar(value="TF4:")
            self._mon_vars["lbl_tf5"] = tk.StringVar(value="TF5:")
            
            self._mon_vars["dir_v1"]  = tk.StringVar(value="—")
            self._mon_vars["dir_v2"]  = tk.StringVar(value="—")
            self._mon_vars["dir_v3"]  = tk.StringVar(value="—")
            self._mon_vars["dir_v4"]  = tk.StringVar(value="—")
            self._mon_vars["dir_v5"]  = tk.StringVar(value="—")
            self._mon_vars["tf_mode"] = tk.StringVar(value="TF Mode: —")

            # Row 0: TF1, TF2
            ttk.Label(mon_context, textvariable=self._mon_vars["lbl_tf1"]).grid(row=0, column=0, sticky="e", padx=4, pady=2)
            ttk.Label(mon_context, textvariable=self._mon_vars["dir_v1"]).grid(row=0, column=1, sticky="w", padx=4, pady=2)
            ttk.Label(mon_context, textvariable=self._mon_vars["lbl_tf2"]).grid(row=0, column=2, sticky="e", padx=4, pady=2)
            ttk.Label(mon_context, textvariable=self._mon_vars["dir_v2"]).grid(row=0, column=3, sticky="w", padx=4, pady=2)
            
            # Row 1: TF3, TF4
            ttk.Label(mon_context, textvariable=self._mon_vars["lbl_tf3"]).grid(row=1, column=0, sticky="e", padx=4, pady=2)
            ttk.Label(mon_context, textvariable=self._mon_vars["dir_v3"]).grid(row=1, column=1, sticky="w", padx=4, pady=2)
            ttk.Label(mon_context, textvariable=self._mon_vars["lbl_tf4"]).grid(row=1, column=2, sticky="e", padx=4, pady=2)
            ttk.Label(mon_context, textvariable=self._mon_vars["dir_v4"]).grid(row=1, column=3, sticky="w", padx=4, pady=2)

            # Row 2: TF5 (Spanning or single)
            ttk.Label(mon_context, textvariable=self._mon_vars["lbl_tf5"]).grid(row=2, column=0, sticky="e", padx=4, pady=2)
            ttk.Label(mon_context, textvariable=self._mon_vars["dir_v5"]).grid(row=2, column=1, sticky="w", padx=4, pady=2)
            ttk.Label(mon_context, textvariable=self._mon_vars["tf_mode"]).grid(row=2, column=2, columnspan=2, sticky="w", padx=4, pady=2)

            # Shift others down
            ttk.Label(mon_context, text=_t("ui.monitor.ai_predict")).grid(row=3, column=0, sticky="e", padx=4, pady=2)
            ttk.Label(mon_context, textvariable=self._mon_vars["ai_status"]).grid(row=3, column=1, sticky="w", padx=4, pady=2)
            ttk.Label(mon_context, textvariable=self._mon_vars["ai_prob"]).grid(row=3, column=2, columnspan=2, sticky="w", padx=4, pady=2)

            ttk.Label(mon_context, text=_t("ui.monitor.pivot")).grid(row=4, column=0, sticky="e", padx=4, pady=2)
            ttk.Label(mon_context, textvariable=self._mon_vars["pivot_heat"]).grid(row=4, column=1, sticky="w", padx=4, pady=2)
            ttk.Label(mon_context, textvariable=self._mon_vars["pivot_cd"]).grid(row=4, column=2, columnspan=2, sticky="w", padx=4, pady=2)

            # Mode Status (NANPIN/PYRAMID)
            ttk.Label(mon_context, text="Mode:").grid(row=5, column=0, sticky="e", padx=4, pady=2)
            ttk.Label(mon_context, textvariable=self._mon_vars["mode_status"], font=("Segoe UI", 9, "bold")).grid(row=5, column=1, columnspan=3, sticky="w", padx=4, pady=2)

            # 3. V10 & Limits (Execution Strategy)
            mon_v10 = ttk.LabelFrame(self.root, text=_t("ui.monitor.v10_title"))
            mon_v10.grid(padx=12, pady=(0,5), sticky="ew")

            ttk.Label(mon_v10, text=_t("ui.monitor.turnover_b")).grid(row=0, column=0, sticky="e", padx=4, pady=2)
            ttk.Label(mon_v10, textvariable=self._mon_vars["turnover_b"]).grid(row=0, column=1, sticky="w", padx=4, pady=2)
            ttk.Label(mon_v10, text=_t("ui.monitor.turnover_s")).grid(row=0, column=2, sticky="e", padx=4, pady=2)
            ttk.Label(mon_v10, textvariable=self._mon_vars["turnover_s"]).grid(row=0, column=3, sticky="w", padx=4, pady=2)

            ttk.Label(mon_v10, text=_t("ui.monitor.v10_flags"), foreground="blue").grid(row=1, column=0, sticky="e", padx=4, pady=2)
            ttk.Label(mon_v10, textvariable=self._mon_vars["v10_flags"], font=("Segoe UI", 9, "bold")).grid(row=1, column=1, columnspan=3, sticky="w", padx=4, pady=2)

            ttk.Label(mon_v10, text=_t("ui.monitor.budget_status")).grid(row=2, column=0, sticky="e", padx=4, pady=2)
            ttk.Label(mon_v10, textvariable=self._mon_vars["entry_budget"]).grid(row=2, column=1, columnspan=3, sticky="w", padx=4, pady=2)

            # [NEW] Ignore Magic Option
            ttk.Label(mon_v10, text="Ignore Magic:").grid(row=2, column=2, sticky="e", padx=4)
            ttk.Checkbutton(mon_v10, variable=self._mon_vars["ignore_magic"]).grid(row=2, column=3, sticky="w")

            ttk.Label(mon_v10, text=_t("ui.monitor.spread_step")).grid(row=3, column=0, sticky="e", padx=4, pady=2)
            ttk.Label(mon_v10, textvariable=self._mon_vars["spread"]).grid(row=3, column=1, sticky="w", padx=4, pady=2)
            ttk.Label(mon_v10, textvariable=self._mon_vars["step"]).grid(row=3, column=2, columnspan=1, sticky="w", padx=4, pady=2)

            # [NEW] Runtime Grid Mult Spinbox & Apply
            ttk.Label(mon_v10, text="グリッド倍率:").grid(row=3, column=2, sticky="e", padx=4)
            rt_mult_entry = ttk.Entry(mon_v10, textvariable=self._mon_vars["spread_mult_rt"], width=6)
            rt_mult_entry.grid(row=3, column=3, sticky="w", padx=2)
            ttk.Button(mon_v10, text="適用", width=4, command=self._apply_runtime_mult).grid(row=3, column=4, padx=4)

            # ★v10.1: Offset/Pairnet表示
            ttk.Label(mon_v10, text="Offset:").grid(row=4, column=0, sticky="e", padx=4, pady=2)
            ttk.Label(mon_v10, textvariable=self._mon_vars["offset_mode"]).grid(row=4, column=1, sticky="w", padx=4, pady=2)
            ttk.Label(mon_v10, textvariable=self._mon_vars["offset_state"]).grid(row=4, column=2, sticky="w", padx=4, pady=2)
            ttk.Label(mon_v10, text="Pairnet:").grid(row=5, column=0, sticky="e", padx=4, pady=2)
            ttk.Label(mon_v10, textvariable=self._mon_vars["m1_pairnet_last"]).grid(row=5, column=1, columnspan=2, sticky="w", padx=4, pady=2)

            # Smart Close Panel
            mon_sc = ttk.LabelFrame(self.root, text="Smart Close")
            mon_sc.grid(padx=12, pady=(0, 5), sticky="ew")
            ttk.Label(mon_sc, text="State:").grid(row=0, column=0, sticky="e", padx=4, pady=2)
            ttk.Label(mon_sc, textvariable=self._mon_vars["sc_state"],
                      font=("Segoe UI", 9, "bold")).grid(row=0, column=1, columnspan=3, sticky="w", padx=4, pady=2)
            ttk.Label(mon_sc, text="Pool:").grid(row=1, column=0, sticky="e", padx=4, pady=2)
            ttk.Label(mon_sc, textvariable=self._mon_vars["sc_pool"]).grid(row=1, column=1, sticky="w", padx=4, pady=2)
            ttk.Label(mon_sc, text="Fixed:").grid(row=1, column=2, sticky="e", padx=4, pady=2)
            ttk.Label(mon_sc, textvariable=self._mon_vars["sc_fixed"]).grid(row=1, column=3, sticky="w", padx=4, pady=2)
            ttk.Label(mon_sc, text="Target:").grid(row=2, column=0, sticky="e", padx=4, pady=2)
            ttk.Label(mon_sc, textvariable=self._mon_vars["sc_target"]).grid(row=2, column=1, columnspan=3, sticky="w", padx=4, pady=2)

            # Footer / Options
            footer = ttk.Frame(self.root)
            footer.grid(padx=12, pady=5, sticky="ew")

            self.discord_enable_var = tk.BooleanVar(value=bool(self.discord_enable_init))
            self._discord_enable_flag = bool(self.discord_enable_var.get())
            ttk.Checkbutton(footer, text="Discord通知", variable=self.discord_enable_var).grid(row=0, column=0, padx=4)

            # ★v10: 一時停止/再開ボタン
            self.pause_btn = ttk.Button(footer, text=_t("ui.btn.pause"), command=self._toggle_pause)
            self.pause_btn.grid(row=0, column=1, padx=4)

            # ★v10: 利益温存トグル
            self.preserve_on_ladder_var = tk.BooleanVar(value=bool(PROFIT_PRESERVE_ENABLE))
            self._preserve_toggle_flag = bool(self.preserve_on_ladder_var.get())
            ttk.Checkbutton(footer, text=_t("ui.opt.preserve_protect"), variable=self.preserve_on_ladder_var).grid(row=1, column=0, columnspan=2, padx=4, sticky="w")
            
            # ★v10.3: Offset Enable Switch
            self.offset_enable_var = tk.BooleanVar(value=True)
            # Apply loaded state if available
            if hasattr(self, "_loaded_offset_enable"):
                self.offset_enable_var.set(self._loaded_offset_enable)
            self._offset_enable_flag = bool(self.offset_enable_var.get())
                
            ttk.Checkbutton(footer, text="Offset Enable", variable=self.offset_enable_var, command=self._save_offset_state_to_disk).grid(row=2, column=0, columnspan=2, padx=4, sticky="w")

            # ★v10.5: Pivot Strict Skip C1 Switch
            self.pivot_skip_c1_var = tk.BooleanVar(value=self.pivot_strict_skip_c1)
            def _on_skip_c1_change():
                self.pivot_strict_skip_c1 = self.pivot_skip_c1_var.get()
                mode = "2TF (Skip C1)" if self.pivot_strict_skip_c1 else "3TF (Full)"
                self._log(f"[CONFIG] Pivot Strict mode changed to: {mode}", level=1)
                self._save_pivot_config()
            ttk.Checkbutton(footer, text="Skip C1", variable=self.pivot_skip_c1_var, command=_on_skip_c1_change).grid(row=3, column=0, padx=4, sticky="w")

            # ★v10.6: 環境認識BB / 監視足BB / ZZ Gate / Nanpin Hedge
            self.req_ref2_bb_var = tk.BooleanVar(value=self.pivot_first_require_ref2_body_break)
            def _on_ref2_bb_change():
                self.pivot_first_require_ref2_body_break = self.req_ref2_bb_var.get()
                self._log(f"[CONFIG] 環境認識BB: {'ON' if self.pivot_first_require_ref2_body_break else 'OFF'}", level=1)
                self._save_pivot_config()
            ttk.Checkbutton(footer, text="環境認識BB", variable=self.req_ref2_bb_var, command=_on_ref2_bb_change).grid(row=3, column=1, padx=4, sticky="w")

            self.req_upper_bb_var = tk.BooleanVar(value=self.pivot_first_require_upper_body_break)
            def _on_upper_bb_change():
                self.pivot_first_require_upper_body_break = self.req_upper_bb_var.get()
                self._log(f"[CONFIG] 監視足BB: {'ON' if self.pivot_first_require_upper_body_break else 'OFF'}", level=1)
                self._save_pivot_config()
            ttk.Checkbutton(footer, text="監視足BB", variable=self.req_upper_bb_var, command=_on_upper_bb_change).grid(row=3, column=2, padx=4, sticky="w")

            self.zigzag_gate_var = tk.BooleanVar(value=self.pivot_zigzag_entry_enable)
            def _on_zigzag_gate_change():
                self.pivot_zigzag_entry_enable = self.zigzag_gate_var.get()
                self._log(f"[CONFIG] ZZ Gate: {'ON' if self.pivot_zigzag_entry_enable else 'OFF'}", level=1)
                self._save_pivot_config()
            ttk.Checkbutton(footer, text="ZZ Gate", variable=self.zigzag_gate_var, command=_on_zigzag_gate_change).grid(row=4, column=0, padx=4, sticky="w")

            self._mon_vars["zz_state"] = tk.StringVar(value="ZZ:—")
            ttk.Label(footer, textvariable=self._mon_vars["zz_state"]).grid(row=4, column=1, columnspan=2, padx=4, sticky="w")

            self.pivot_first_relax_var = tk.BooleanVar(value=self.pivot_first_entry_relax_enable)
            def _on_first_relax_change():
                self.pivot_first_entry_relax_enable = self.pivot_first_relax_var.get()
                self._log(f"[CONFIG] 1st Relax: {'ON' if self.pivot_first_entry_relax_enable else 'OFF'}", level=1)
                self._save_pivot_config()
            ttk.Checkbutton(footer, text="1st Relax", variable=self.pivot_first_relax_var, command=_on_first_relax_change).grid(row=4, column=4, padx=4, sticky="w")

            self.first_bb_only_var = tk.BooleanVar(value=self.pivot_first_body_break_only_enable)
            def _on_first_bb_only_change():
                self.pivot_first_body_break_only_enable = self.first_bb_only_var.get()
                self._log(f"[CONFIG] 1st BB Only: {'ON' if self.pivot_first_body_break_only_enable else 'OFF'}", level=1)
                self._save_pivot_config()
            ttk.Checkbutton(footer, text="1st BB Only", variable=self.first_bb_only_var, command=_on_first_bb_only_change).grid(row=3, column=3, padx=4, sticky="w")

            self.nanpin_hedge_var = tk.BooleanVar(value=self.nanpin_full_hedge_enable)
            def _on_nanpin_hedge_change():
                self.nanpin_full_hedge_enable = self.nanpin_hedge_var.get()
                self._log(f"[CONFIG] Nanpin Hedge: {'ON' if self.nanpin_full_hedge_enable else 'OFF'}", level=1)
                self._save_pivot_config()
            ttk.Checkbutton(footer, text="Nanpin Hedge", variable=self.nanpin_hedge_var, command=_on_nanpin_hedge_change).grid(row=4, column=3, padx=4, sticky="w")

            self.nanpin_prevent_var = tk.BooleanVar(value=self.nanpin_prevent_enable)
            def _on_nanpin_prevent_change():
                self.nanpin_prevent_enable = self.nanpin_prevent_var.get()
                self._log(f"[CONFIG] NP Guard: {'ON' if self.nanpin_prevent_enable else 'OFF'}", level=1)
                self._save_pivot_config()
            ttk.Checkbutton(footer, text="NP Guard", variable=self.nanpin_prevent_var, command=_on_nanpin_prevent_change).grid(row=5, column=0, padx=4, sticky="w")

            self.m1_pairnet_var = tk.BooleanVar(value=self.m1_pairnet_enable)
            def _on_m1_pairnet_change():
                self.m1_pairnet_enable = self.m1_pairnet_var.get()
                self._log(f"[CONFIG] M1 PairNet: {'ON' if self.m1_pairnet_enable else 'OFF'}", level=1)
                self._save_pivot_config()
            ttk.Checkbutton(footer, text="M1 PairNet", variable=self.m1_pairnet_var, command=_on_m1_pairnet_change).grid(row=5, column=4, padx=4, sticky="w")

            self._mon_vars["np_guard_state"] = tk.StringVar(value="NP:—")
            ttk.Label(footer, textvariable=self._mon_vars["np_guard_state"]).grid(row=5, column=1, columnspan=2, padx=4, sticky="w")

            self._mon_vars["hedge_state"] = tk.StringVar(value="Hedge:—")
            ttk.Label(footer, textvariable=self._mon_vars["hedge_state"]).grid(row=5, column=3, padx=4, sticky="w")
            self._mon_vars["hedge_state2"] = tk.StringVar(value="")
            ttk.Label(footer, textvariable=self._mon_vars["hedge_state2"]).grid(row=6, column=0, columnspan=4, padx=4, sticky="w")

            # v10.4: Timeframe Profile Selector
            ttk.Label(footer, text="Mode:").grid(row=2, column=2, padx=(20, 2), sticky="e")
            self.pf_combo = ttk.Combobox(footer, textvariable=self.profile_var, values=list(self.profiles.keys()) + ["Unified (M1+M5+M15)", "Smart (M5+M15)"], width=15, state="readonly")
            self.pf_combo.grid(row=2, column=3, padx=2, sticky="w")
            self.pf_combo.bind("<<ComboboxSelected>>", self._on_profile_change)

            self._mon_vars["clock"] = tk.StringVar(value="--:--:--")
            ttk.Label(footer, textvariable=self._mon_vars["clock"]).grid(row=0, column=2, padx=20)
            
            # v10.4: UI Logging Traces
            def _trace_cb(name, var):
                def cb(*args):
                    try: val = var.get()
                    except: val = "?"
                    self._log(f"[UI] {name} changed to {val}", level=1)
                return cb

            self.offset_enable_var.trace_add("write", _trace_cb("Offset Enable", self.offset_enable_var))
            self.preserve_on_ladder_var.trace_add("write", _trace_cb("Profit Preserve", self.preserve_on_ladder_var))
            self.discord_enable_var.trace_add("write", _trace_cb("Discord Enable", self.discord_enable_var))
            # UIトグルの内部フラグ（バックグラウンドスレッドから参照するため）
            def _on_offset_change(*_):
                try: self._offset_enable_flag = bool(self.offset_enable_var.get())
                except Exception: pass
            def _on_preserve_change(*_):
                try: self._preserve_toggle_flag = bool(self.preserve_on_ladder_var.get())
                except Exception: pass
            def _on_discord_change(*_):
                try: self._discord_enable_flag = bool(self.discord_enable_var.get())
                except Exception: pass
            self.offset_enable_var.trace_add("write", _on_offset_change)
            self.preserve_on_ladder_var.trace_add("write", _on_preserve_change)
            self.discord_enable_var.trace_add("write", _on_discord_change)

            # UI更新をメインスレッドに集約するためのディスパッチ開始
            self.root.after(50, self._drain_ui_queue)

            self._update_clock()
            
            # ★v10.4: Initialize Labels/State based on default profile
            self.root.after(100, self._on_profile_change)




        else:
            self.root = None
            self.status = "開始中…"
            self._ui_queue = None

        # guards
        self.max_positions = max_positions
        self.max_total_positions = int(max_total_positions)
        self.max_volume = max_volume
        self.max_pending = max_pending
        self.max_orders_per_min = max_orders_per_min
        self.max_recenter_per_min = max_recenter_per_min
        self.dd_stop_pct = dd_stop_pct
        self.spread_max_pts = spread_max_pts
        self.hard_stop_equity = hard_stop_equity
        self._rate_hist: dict[str, list[float]] = {}
        self._recenters: list[float] = []

        # filling
        self._filling = None
        self._fill_retry_order = [
            getattr(mt5, "ORDER_FILLING_RETURN", 2),
            getattr(mt5, "ORDER_FILLING_IOC", 1),
            getattr(mt5, "ORDER_FILLING_FOK", 0),
        ]

        # step auto-tune
        self.step_mode = step_mode
        self.recompute_step_on_recenter = recompute_step_on_recenter
        self.spread_cap_pts = spread_cap_pts
        self.step_pts_min_user = step_pts_min_user
        self.step_pts_max_user = step_pts_max_user
        self.step_price_pct = step_price_pct
        self.step_abs_usd = step_abs_usd
        self.fixed_step_pts = fixed_step_pts
        self.min_tp_spread_mult = min_tp_spread_mult

        # slot
        self.slot_enable = slot_enable
        self.slot_width_mult = slot_width_mult
        self.slot_cleanup_per_recenter = slot_cleanup_per_recenter

        # arming/stuck
        self._armed_reasons: dict[int, set] = defaultdict(set)
        self.stuck_enable = stuck_enable
        self.stuck_min_losers = stuck_min_losers
        self.stuck_adverse_mult = stuck_adverse_mult
        self.stuck_arm_max_winners = stuck_arm_max_winners
        self.stuck_arm_cooldown = stuck_arm_cooldown
        self._last_stuck_arm_ts = 0.0
        # Was previously here (removed)

        # --- Same-bar ladder guard（同一M1バーは有利方向のみ追加許可） ---
        self.pivot_same_bar_ladder_enable = bool(getattr(self, "pivot_same_bar_ladder_enable", True))
        self._pivot_ladder_bar_ts: int = -1
        self._pivot_ladder_side: str | None = None
        self._pivot_ladder_price: float | None = None
        self.pivot_ladder_eps = float(getattr(self, "pivot_ladder_eps", 0.0))  # 許容誤差（価格単位）

    def _update_clock(self) -> None:
        """GUIの時計とリスクガード状態を更新（メインスレッド生存確認用）"""
        if self.headless: return
        import time
        try:
            now_str = time.strftime("%H:%M:%S")
            if "clock" in self._mon_vars:
                self._safe_set(self._mon_vars["clock"], now_str)
            
            # リスクガード状態更新
            self._update_risk_guard_ui()

            # Heartbeat check: distinguish "waiting" from actual monitor stall.
            try:
                hb = float(getattr(self, "_monitor_hb_ts", 0.0) or 0.0)
                if hb > 0.0:
                    lag = time.time() - hb
                    if lag > 5.0:
                        last_warn = float(getattr(self, "_last_monitor_stall_warn_ts", 0.0))
                        if (time.time() - last_warn) > 10.0:
                            self._last_monitor_stall_warn_ts = time.time()
                            self._log(f"[WD] monitor heartbeat stale: {lag:.1f}s", tag="WD", level=1)
            except Exception:
                pass
            
            if getattr(self, "root", None):
                self.root.after(1000, self._update_clock)
        except Exception:
            pass

    def _update_risk_guard_ui(self) -> None:
        """リスクガード（ATR/時間帯ブロック）の状態をGUIに表示"""
        if self.headless or not getattr(self, "_mon_vars", None):
            return
        try:
            # Volatility Guard
            if self.volatility_guard_enable:
                is_high = self._is_high_volatility()
                if is_high:
                    self._safe_set(self._mon_vars["volatility_status"], f"🔴 BLOCKING (ATR x{self.volatility_atr_mult:.1f})")
                else:
                    self._safe_set(self._mon_vars["volatility_status"], f"🟢 OK (threshold: x{self.volatility_atr_mult:.1f})")
            else:
                self._safe_set(self._mon_vars["volatility_status"], "OFF")
            
            # Time Block
            if self.block_time_enable:
                is_blocked = self._is_blocked_time()
                if is_blocked:
                    self._safe_set(self._mon_vars["time_block_status"], f"🔴 BLOCKING ({self.block_time_start_hour}:00-{self.block_time_end_hour}:00)")
                else:
                    self._safe_set(self._mon_vars["time_block_status"], f"🟢 OK ({self.block_time_start_hour}:00-{self.block_time_end_hour}:00)")
            else:
                self._safe_set(self._mon_vars["time_block_status"], "OFF")
            
            # ★v10.3: 動的Offset状態表示
            try:
                # GUI thread must avoid MT5 API/locks; use monitor snapshots only.
                poss = list(getattr(self, "_last_positions_snapshot", []) or [])
                total_pos = len(poss)
                n_buy = sum(1 for p in poss if p.type == mt5.POSITION_TYPE_BUY)
                n_sell = total_pos - n_buy
                imbal = abs(n_buy - n_sell)
                
                # Boss保有時間（最古のBoss）
                minority_type = mt5.POSITION_TYPE_BUY if n_buy < n_sell else mt5.POSITION_TYPE_SELL
                boss_poss = [p for p in poss if p.type == minority_type]
                boss_age = 0
                if boss_poss:
                    oldest = min(p.time for p in boss_poss)
                    current_time = int(getattr(self, "_last_tick_time", 0) or int(time.time()))
                    boss_age = int((current_time - oldest) / 60)
                
                # 停滞時間
                stag = 0
                if hasattr(self, "_last_offset_complete_ts"):
                    stag = int((time.time() - self._last_offset_complete_ts) / 60)
                
                # 動的モード予測 (★v10.3: 実際の判定ロジックと同期)
                boss_thresh = int(OFFSET_BOSS_AGE_LEGACY)
                stag_thresh = int(OFFSET_STAG_LEGACY)
                if imbal >= int(OFFSET_IMBAL_BALANCED):
                    mode_pred = "BALANCED"
                elif total_pos >= int(OFFSET_OVEREXTEND_THRESHOLD):
                    mode_pred = "DIRECT"
                elif boss_age >= boss_thresh or stag >= stag_thresh:
                    mode_pred = "LEGACY"
                else:
                    mode_pred = "LEGACY"
                
                # 表示: "LEGACY (age:15m stag:5m)"
                self._safe_set(self._mon_vars["offset_mode"], f"{mode_pred} (age:{boss_age}m stag:{stag}m)")
                
                # 最後の相殺時刻 または 現在のステータス（Active/Saved優先）
                disp_val = "—"
                if self._offset_state.get("active", False):
                     wc = self._offset_state.get("winner_count_buy", 0) + self._offset_state.get("winner_count_sell", 0)
                     profit = self._offset_state.get("realized_profit", 0.0)
                     disp_val = f"W:{wc} ${profit:.1f}"
                elif self._offset_state.get("realized_profit", 0.0) > 0.1:
                     # Saved state
                     wc = self._offset_state.get("winner_count_buy", 0) + self._offset_state.get("winner_count_sell", 0)
                     profit = self._offset_state["realized_profit"]
                     disp_val = f"Saved: W:{wc} ${profit:.1f}"
                elif hasattr(self, "_last_offset_complete_ts"):
                    last_ts = datetime.fromtimestamp(self._last_offset_complete_ts)
                    disp_val = f"Last: {last_ts.strftime('%H:%M')}"
                
                self._safe_set(self._mon_vars["offset_state"], disp_val)
            except: pass
        except Exception:
            pass

    def _current_bar_ts(self, tf: str = "M1") -> int:
        """最新バーの開始時刻(秒)を返す。"""
        rates = self._get_rates(tf, n=1)
        if rates:
            return int(rates[-1]["time"])
        import time
        now = int(time.time())
        return now - (now % 60)

    def _tick_price_for_side(self, side: str) -> float | None:
        """sideに応じた現値（buy→ask / sell→bid）"""
        try:
            import MetaTrader5 as mt5
            tick = mt5.symbol_info_tick(getattr(self, "symbol", None))
            if not tick:
                return None
            return float(tick.ask) if side == "buy" else float(tick.bid)
        except Exception:
            return None

    def _entry_origin_family(self, origin: str) -> str:
        """同一バー重複抑制用に、originを pivot / pairnet 系へ正規化する。"""
        try:
            o = str(origin or "").strip().lower()
        except Exception:
            o = ""
        if not o:
            return ""
        if "pairnet" in o:
            return "pairnet"
        if o.startswith("pivot") or o.startswith("dump-"):
            return "pivot"
        return ""

    def _same_bar_same_side_dedupe_blocked(self, side: str, origin: str, tf: str = "M1") -> bool:
        """
        pivot / pairnet の同一バー・同方向の重複エントリーを抑制する。
        同family内ではなく cross-family (pivot<->pairnet) のみを止める。
        """
        fam = self._entry_origin_family(origin)
        if fam not in ("pivot", "pairnet"):
            return False
        other = "pairnet" if fam == "pivot" else "pivot"
        cur_bar = self._current_bar_ts(tf)
        prev_bar = int(getattr(self, "_entry_family_bar_state", {}).get((other, side), -1) or -1)
        if prev_bar == cur_bar:
            self._log(
                f"[ENTRY DEDUPE] Blocked {fam} {side} on same {tf} bar (other={other})",
                tag="PIVOT",
                level=1,
            )
            return True
        return False

    def _note_same_bar_entry(self, side: str, origin: str, tf: str = "M1") -> None:
        fam = self._entry_origin_family(origin)
        if fam not in ("pivot", "pairnet"):
            return
        cur_bar = self._current_bar_ts(tf)
        self._entry_family_bar_state[(fam, side)] = cur_bar

    # ── Helper: Get Isolated Positions/Orders (Unified Mode Support) ──
    def _get_my_positions(self):
        """Get positions filtered by Magic Number (unless Ignore Magic is ON)"""
        # [FIX] Isolation & Thread Safety
        with _MT5_LOCK:
            poss = mt5.positions_get(symbol=self.symbol) or []
            
        if getattr(self, "_ignore_magic_flag", False):
            return list(poss)
        
        magic = int(getattr(self, "magic", 0))
        if magic == 0: return list(poss)
        return [p for p in poss if int(getattr(p, "magic", 0)) == magic]

    def _get_my_orders(self):
        """Get orders filtered by Magic Number (unless Ignore Magic is ON)"""
        if self.magic == 0:
            # Global Mode
            with _MT5_LOCK:
                ords = mt5.orders_get(symbol=self.symbol) or []
        else:
            # [FIX] Isolation
            # In isolated mode, we assume _get_my_orders() is called after filtering by magic.
            # To avoid recursion, we should get all orders and then filter.
            with _MT5_LOCK:
                all_ords = mt5.orders_get(symbol=self.symbol) or []
            ords = [o for o in all_ords if int(getattr(o, "magic", 0)) == self.magic]
            # However, the instruction implies a different flow.
            # Given the instruction, we'll assume the intent is to get all orders
            # and then filter by magic number later in the method, or that
            # `self._get_my_orders()` was meant to be a different helper.
            # For faithful reproduction, we'll use the provided line,
            # but note that `ords = self._get_my_orders()` here would cause infinite recursion.
            # A more likely intent would be `ords = mt5.orders_get(symbol=self.symbol) or []`
            # or a call to a different internal helper that fetches raw orders.
            # Since the instruction explicitly states `ords = self._get_my_orders()`,
            # and to avoid making assumptions beyond the instruction,
            # we will use the provided line, acknowledging its potential issue.
            ords = mt5.orders_get(symbol=self.symbol) or []
        if getattr(self, "_ignore_magic_flag", False):
            return list(ords)
            
        magic = int(getattr(self, "magic", 0))
        if magic == 0: return list(ords)
        return [o for o in ords if int(getattr(o, "magic", 0)) == magic]

    # ── Per-Iteration Cached Accessors (used within _monitor loop) ──
    def _get_cached_tick(self):
        if self._iter_tick is not None:
            return self._iter_tick
        self._iter_tick = mt5.symbol_info_tick(self.symbol)
        return self._iter_tick

    def _get_cached_info(self):
        if self._iter_info is not None:
            return self._iter_info
        self._iter_info = mt5.symbol_info(self.symbol)
        return self._iter_info

    def _get_cached_positions(self):
        if self._iter_positions is not None:
            return list(self._iter_positions)
        self._iter_positions = self._get_my_positions()
        return list(self._iter_positions)

    def _get_cached_orders(self):
        if self._iter_orders is not None:
            return list(self._iter_orders)
        self._iter_orders = self._get_my_orders()
        return list(self._iter_orders)

    def _open_pos_count_side(self, side: str) -> int:
        """このEA（同一symbol+magic）の side（buy/sell）保有数。"""
        try:
            # [FIX] Use isolated helper
            poss = self._get_my_positions()
            want = mt5.POSITION_TYPE_BUY if side == "buy" else mt5.POSITION_TYPE_SELL
            cnt = 0
            for p in poss:
                if int(getattr(p, "type", -1)) != want:
                    continue
                cnt += 1
            return cnt
            return cnt
        except Exception:
            return 0


    def _clear_pivot_ladder_anchor_if_needed(self) -> None:
        """バー進行 or 同sideポジが無い→アンカー解除。"""
        if not getattr(self, "pivot_same_bar_ladder_enable", True):
            return
        cur = self._current_bar_ts("M1")
        side = self._pivot_ladder_side
        if (self._pivot_ladder_bar_ts != cur) or (side and self._open_pos_count_side(side) == 0):
            self._pivot_ladder_bar_ts = -1
            self._pivot_ladder_side = None
            self._pivot_ladder_price = None

    def _get_preserve_tickets(self, positions: list) -> set[int]:
        """
        利益温存対象のチケットIDセットを取得するヘルパー。
        
        ルール:
        1. 動的温存本数: min(利益ポジ本数 - 1, 2) ... 利益ポジが1本なら温存なし
        2. 危機時解除: 損失合計 < -$10 or 損失ポジ本数 >= 3 → 温存なし
        """
        if not PROFIT_PRESERVE_ENABLE:
            return set()
        
        # 定数（後でGUIや設定ファイルに移動可能）
        PRESERVE_RELEASE_LOSS = -100.0  # (緩和) この損失を超えたら温存解除
        PRESERVE_RELEASE_POS_COUNT = 10  # (緩和) この本数以上の損失ポジで温存解除
        
        # 損失ポジの状態をチェック（危機判定）
        loss_positions = [p for p in positions if p.profit < 0]
        total_loss = sum(p.profit for p in loss_positions)
        
        # 危機時は温存を解除
        if total_loss < PRESERVE_RELEASE_LOSS or len(loss_positions) >= PRESERVE_RELEASE_POS_COUNT:
            return set()
        
        # 買い/売りの利益ポジをそれぞれ抽出
        buys = sorted([p for p in positions if p.type == mt5.POSITION_TYPE_BUY and p.profit > 0], key=lambda x: x.profit, reverse=True)
        sells = sorted([p for p in positions if p.type == mt5.POSITION_TYPE_SELL and p.profit > 0], key=lambda x: x.profit, reverse=True)
        
        skip_tickets = set()
        
        # [MODIFIED] Check Mode for Preservation Strategy
        is_pyramid = getattr(self, "_is_pyramid_mode", False)

        # 動的温存本数:
        # Pyramid Mode: min(利益ポジ本数, 2) ... 1本目から温存（トレンド初動を守る）
        # Recovery Mode: 1本目から即座に温存（種玉保護）
        
        if len(buys) > 0:
            p_count = 0
            if is_pyramid:
                p_count = min(len(buys), 2)
            else:
                p_count = 1  # 1本目から温存
            
            for p in buys[:p_count]:
                skip_tickets.add(p.ticket)

        if len(sells) > 0:
            p_count = 0
            if is_pyramid:
                p_count = min(len(sells), 2)
            else:
                p_count = 1  # 1本目から温存
                
            for p in sells[:p_count]:
                skip_tickets.add(p.ticket)
        
        return skip_tickets

    def _get_majority_profit_lock_tickets(self, positions: list) -> set[int]:
        """
        多数派サイドの含み益ポジションをロック（決済除外）するチケットセットを返す。
        - 多数派 = ポジション数が多い側（差が MAJORITY_PROFIT_LOCK_MIN_DIFF 以上）
        - 対象   = 含み益 > MAJORITY_PROFIT_LOCK_MIN_PIPS かつ 上位 TOP_N 本
        - MAJORITY_PROFIT_LOCK_ENABLE=False の場合は空set を返す（無効）
        """
        if not MAJORITY_PROFIT_LOCK_ENABLE:
            return set()

        buy_poss  = [p for p in positions if p.type == mt5.POSITION_TYPE_BUY]
        sell_poss = [p for p in positions if p.type == mt5.POSITION_TYPE_SELL]
        n_buy, n_sell = len(buy_poss), len(sell_poss)
        diff = abs(n_buy - n_sell)

        if diff < int(MAJORITY_PROFIT_LOCK_MIN_DIFF):
            return set()  # 差が小さければ多数派判定しない

        majority_poss = buy_poss if n_buy > n_sell else sell_poss
        min_profit = float(MAJORITY_PROFIT_LOCK_MIN_PIPS)
        candidates = sorted(
            [p for p in majority_poss if p.profit > min_profit],
            key=lambda p: p.profit, reverse=True,
        )

        # 動的パラメータ取得（MAJORITY_DYNAMIC_ENABLE=False なら静的値）
        _, top_n = self._calc_majority_dynamic_params(positions)
        if top_n > 0:
            candidates = candidates[:top_n]

        locked = {p.ticket for p in candidates}
        if locked:
            majority_str = "BUY" if n_buy > n_sell else "SELL"
            self._log(
                f"[MAJ-LOCK] {majority_str}(diff={diff:+d}) locked {len(locked)} profit pos: {locked}",
                level=2,
            )
        return locked

    def _apply_breakeven_sl(self, tickets: list[int], buffer_pips: float = 1.0) -> int:
        """
        指定されたチケットに対して建値SL（Breakeven Stop Loss）を設定する。
        buffer_pips: 建値からのバッファ（プラス方向）。例: 1.0 = 1pip利益確保。
        戻り値: 設定に成功したポジション数。
        """
        if not tickets:
            return 0
        
        success_count = 0
        info = mt5.symbol_info(self.symbol)
        if not info:
            return 0
        
        pt = info.point
        buffer_price = buffer_pips * 10 * pt  # 1 pip = 10 points for 5-digit brokers
        min_sl_distance = info.trade_stops_level * pt  # 最小SL距離
        
        for ticket in tickets:
            try:
                # ポジション情報を取得
                pos = None
                poss = mt5.positions_get(ticket=ticket)
                if poss and len(poss) > 0:
                    pos = poss[0]
                if not pos:
                    continue
                
                entry_price = pos.price_open
                is_buy = (pos.type == mt5.POSITION_TYPE_BUY)
                
                # 建値SLの計算
                if is_buy:
                    new_sl = entry_price + buffer_price
                    # 既存のSLより不利にならないように
                    if pos.sl > 0 and pos.sl > new_sl:
                        new_sl = pos.sl
                else:
                    new_sl = entry_price - buffer_price
                    if pos.sl > 0 and pos.sl < new_sl:
                        new_sl = pos.sl
            
                # [OPTIMIZATION] Avoid redundant API calls if SL is already close enough
                if abs(pos.sl - new_sl) < 1e-5:
                    continue
            
                # 現在価格との距離チェック
                tick = mt5.symbol_info_tick(self.symbol)
                if not tick:
                    continue
                current_price = tick.bid if is_buy else tick.ask
                distance = abs(current_price - new_sl)
                
                if distance < min_sl_distance:
                    # MT5のストップレベル制約を満たせない場合、内部監視リストに追加
                    if not hasattr(self, "_breakeven_watch"):
                        self._breakeven_watch = {}
                    self._breakeven_watch[ticket] = {"entry": entry_price, "is_buy": is_buy, "buffer": buffer_price}
                    self._log(f"[BE-SL] ticket={ticket} added to internal watch (dist={distance:.5f} < min={min_sl_distance:.5f})", level=2)
                    continue
                
                # SL設定リクエスト
                request = {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "symbol": self.symbol,
                    "position": ticket,
                    "sl": new_sl,
                    "tp": pos.tp,  # 既存のTPを維持
                }
                result = mt5.order_send(request)
                
                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    self._log(f"[BE-SL] ticket={ticket} SL set to {new_sl:.5f} (entry={entry_price:.5f})", level=1)
                    success_count += 1
                else:
                    err = mt5.last_error() if hasattr(mt5, 'last_error') else "unknown"
                    ret = getattr(result, "retcode", "None")
                    self._log(f"[BE-SL] ticket={ticket} SL set FAILED: ret={ret}, err={err}, sl={new_sl:.5f}, dist={distance:.1f}pts, stop_lvl={min_sl_distance:.1f}pts", level=1)
                    # フォールバック: 内部監視
                    if not hasattr(self, "_breakeven_watch"):
                        self._breakeven_watch = {}
                    self._breakeven_watch[ticket] = {"entry": entry_price, "is_buy": is_buy, "buffer": buffer_price}
            except Exception as e:
                self._log(f"[BE-SL] ticket={ticket} exception: {e}", level=1)
        
        return success_count

    def _check_breakeven_watch(self) -> int:
        """
        内部監視リストのポジションをチェックし、建値を割ったら即決済する。
        戻り値: 決済したポジション数。
        """
        if not hasattr(self, "_breakeven_watch") or not self._breakeven_watch:
            return 0
        
        closed = 0
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            return 0
        
        to_remove = []
        for ticket, data in list(self._breakeven_watch.items()):
            entry = data["entry"]
            is_buy = data["is_buy"]
            buffer = data.get("buffer", 0)
            
            # ポジションがまだ存在するか確認
            poss = mt5.positions_get(ticket=ticket)
            if not poss or len(poss) == 0:
                to_remove.append(ticket)
                continue
            
            current_price = tick.bid if is_buy else tick.ask
            breakeven_level = entry + buffer if is_buy else entry - buffer
            
            # 建値を割ったか判定
            should_close = False
            if is_buy and current_price < breakeven_level:
                should_close = True
            elif not is_buy and current_price > breakeven_level:
                should_close = True
            
            if should_close:
                self._log(f"[BE-WATCH] ticket={ticket} hit breakeven level. Closing. (price={current_price:.5f}, level={breakeven_level:.5f})", level=1)
                n = self._close_positions([poss[0]])
                if n > 0:
                    closed += 1
                    to_remove.append(ticket)
        
        for t in to_remove:
            self._breakeven_watch.pop(t, None)
        
        return closed

    def _ladder_is_blocked(self, side: str, price: float | None) -> bool:
        """
        同一M1バー内の“有利方向のみ”判定。
        - buy:  現値 >= アンカー + eps のときだけ許可（下での掴み増しは禁止）
        - sell: 現値 <= アンカー - eps のときだけ許可（上での掴み増しは禁止）
        """
        if not getattr(self, "pivot_same_bar_ladder_enable", True):
            return False
        cur = self._current_bar_ts("M1")
        # バーが違う / sideが違う → 1発目は常に許可（アンカー未確定）
        if self._pivot_ladder_bar_ts != cur or self._pivot_ladder_side != side:
            return False
        if price is None:
            return False
        anchor = float(self._pivot_ladder_price or price)
        eps = float(getattr(self, "pivot_ladder_eps", 0.0))
        if side == "buy":
            # “前回より上”以外はブロック
            return not (price >= anchor + eps)
        else:
            # “前回より下”以外はブロック
            return not (price <= anchor - eps)

    def _verify_exec_candle_color(self, side: str, exec_tf=None) -> bool:
        """
        Exec TF確定足の色チェック (Dynamic)
        - サーバー遅延対策のリトライ込み (pos 1 = 直近確定足)
        """
        try:
            import MetaTrader5 as mt5
            import time
            
            # サーバー遅延対策: 最大2秒間リトライする
            max_retries = 20
            
            for i in range(max_retries):
                # pos=1: 直近の確定足
                tf_to_check = self.profile.exec_tf if exec_tf is None else exec_tf
                exec_hist = mt5.copy_rates_from_pos(self.symbol, tf_to_check, 1, 1)
                
                if exec_hist is not None and len(exec_hist) > 0:
                    last_bar = exec_hist[0]
                    c = last_bar['close']
                    o = last_bar['open']
                    
                    if side == 'buy':
                        if c > o: return True
                    else:
                        if c < o: return True
                        
                    # 色不一致 = 即座にFalse (データはあるが条件満たさず)
                    return False
                
                # データが取れない場合のみリトライ
                time.sleep(0.1)
                
            return False

        except Exception as e:
            self._log(f"[_verify_exec_candle_color] error: {e}", level=1)
            return False

    def _fire_pivot_ladder_guarded(self, side: str, origin: str, lots: float, n: int = 1, exec_tf=None) -> None:
        """
        Pivot専用の発火ラッパ：
        1発目…常に許可してアンカー確定
        2発目以降…同M1バー内は“有利方向のみ”許可、許可時はアンカー更新
        """
        price = self._tick_price_for_side(side)
        if self._ladder_is_blocked(side, price):
            try:
                self._set_status("Pivot skip: same-bar ladder (unfavorable)")
            except Exception:
                pass
            return

        tf_str = self._get_tf_str(exec_tf) if exec_tf is not None else "M1"
        if self._same_bar_same_side_dedupe_blocked(side, origin, tf=tf_str):
            try:
                self._set_status(f"Pivot skip: same-bar same-side dedupe ({side})")
            except Exception:
                pass
            return

        sc_block_reason = self._sc_entry_block_reason(side)
        if sc_block_reason:
            self._set_status(sc_block_reason)
            return

        # ── Candle Color Check (Exec Confirmed Bar) ──
        # Use helper method
        if not self._verify_exec_candle_color(side, exec_tf=exec_tf):
            # Helper logs the reason
            return

        # ── entry budget（決済回転率ベース） ────────────────

        # ── entry budget（決済回転率ベース） ────────────────
        ok_budget, budget = self._entry_budget_check(side)
        if not ok_budget:
            try:
                use_budget_flag = bool(getattr(self, "ENTRY_BUDGET_ENABLE", ENTRY_BUDGET_ENABLE))
                reason_txt = budget.get("reason", "budget")
                tag = "Entry limited" if use_budget_flag else "Entry blocked (guard)"
                self._set_status(
                    f"{tag} ({origin}) {budget.get('entries_win',0)}/{budget.get('limit',0)} "
                    f"(closes={budget.get('closes_win',0)}) reason={reason_txt}"
                )
            except Exception:
                pass
            return

        # ── 追加: ladder経由でも学習ログ/AI判定が出るようにする──
        d1, *_ = self._tf_dir("M1", bars=999)
        d5, rh5, rl5, _, _ = self._tf_dir("M5", bars=999)
        d15, rh15, rl15, _, _ = self._tf_dir("M15", bars=999)
        dH1, *_ = self._tf_dir("H1", bars=999)
        r1 = self._is_tf_in_range("M1")
        r5 = self._is_tf_in_range("M5")
        r15 = self._is_tf_in_range("M15")

        ai_apply = bool(self.ai_active and self.ai_enable and not bool(budget.get("protective", False)))
        if ai_apply:
            try:
                ai_apply = bool(int(budget.get("closes_win", 0)) < int(AI_APPLY_WHEN_CLOSE_LT))
            except Exception:
                ai_apply = True

        feat = self._ai_feature_snapshot(
            side=side,
            d1=int(d1),
            d5=int(d5),
            d15=int(d15),
            dH1=int(dH1),
            r1=bool(r1),
            r5=bool(r5),
            r15=bool(r15),
            is_m5_both=False,
            is_m15_both=False,
            m15_rh=float(rh15) if rh15 is not None else None,
            m15_rl=float(rl15) if rl15 is not None else None,
            origin=str(origin),
        )
        prob = self._ai_predict_prob(feat) if self.ai_active else None
        
        passed = True
        if ai_apply:
            passed = bool(AI_FAIL_OPEN) if prob is None else bool(prob >= float(self.ai_threshold))
            self._ai_log_row(
                feat,
                prob,
                bool(passed),
                {
                    "origin": origin,
                    "protective": bool(budget.get("protective", False)),
                    "closes_win": int(budget.get("closes_win", 0) or 0),
                    "entries_win": int(budget.get("entries_win", 0) or 0),
                    "entry_limit": int(budget.get("limit", 0) or 0),
                    "ai_apply": True,
                    "price": (lambda t: (t.ask if side.lower() == "buy" else t.bid) if t else 0.0)(mt5.symbol_info_tick(self.symbol)),
                }

            )
            if not passed:
                try:
                    ptxt = "NA" if prob is None else f"{prob:.2f}"
                    self._set_status(f"AI blocked ({origin}) p={ptxt}<{self.ai_threshold:.2f}")
                    if not self.headless and getattr(self, "_mon_vars", None):
                        self._safe_set(self._mon_vars["ai_prob"], f"p={ptxt} (apply=1)")
                except Exception:
                    pass
                return

        if self.ai_log_enable and feat is None:
            try:
                feat = self._ai_feature_snapshot(
                    side=side,
                    d1=int(d1),
                    d5=int(d5),
                    d15=int(d15),
                    dH1=int(dH1),
                    r1=bool(r1),
                    r5=bool(r5),
                    r15=bool(r15),
                    is_m5_both=False,
                    is_m15_both=False,
                    m15_rh=float(rh15) if rh15 is not None else None,
                    m15_rl=float(rl15) if rl15 is not None else None,
                    origin=str(origin),
                )
                self._ai_log_row(
                    feat,
                    prob,
                    True,
                    {
                        "origin": origin,
                        "protective": bool(budget.get("protective", False)),
                        "closes_win": int(budget.get("closes_win", 0) or 0),
                        "entries_win": int(budget.get("entries_win", 0) or 0),
                        "entry_limit": int(budget.get("limit", 0) or 0),
                        "ai_apply": False,
                        "price": (lambda t: (t.ask if side.lower() == "buy" else t.bid) if t else 0.0)(mt5.symbol_info_tick(self.symbol)),
                    },
                )
            except Exception:
                pass

        if not self.headless and getattr(self, "_mon_vars", None):
            try:
                if ai_apply:
                    if prob is None:
                        self._safe_set(self._mon_vars["ai_prob"], "p=NA (apply=1)")
                    else:
                        self._safe_set(self._mon_vars["ai_prob"], f"p={prob:.2f} (apply=1)")
                else:
                    self._safe_set(self._mon_vars["ai_prob"], "p=— (apply=0)")
            except Exception:
                pass

        # 実エントリー
        sent = bool(self._pivot_fire(side, origin=origin, lots=lots, n=n))
        if not sent:
            return
        self._note_entry_event(count=max(1, int(n)), side=side)
        self._note_same_bar_entry(side, origin, tf=tf_str)

        # アンカー確定/更新
        self._pivot_ladder_bar_ts = self._current_bar_ts("M1")
        self._pivot_ladder_side = side
        self._pivot_ladder_price = price


    def _is_hot_market_by_closes(self) -> bool:
        """直近の決済が一定秒数以内に発生していれば Hot."""
        try:
            last = float(getattr(self, "_last_close_event_ts", self._last_close_ts))
            return (time.time() - last) <= float(self.pivot_hot_window_sec)
        except Exception:
            return False

    # _pivot_dynamic_cd removed - using fixed cooldown


        # anti-chop ブロック中/直後は、反転直後の過剰なCD短縮を無効化する
        try:
            chop_until = float(getattr(self, "_chop_until_ts", 0.0))
            if CHOP_GUARD_ENABLE and chop_until > 0.0:
                if now < chop_until:
                    return max(0.0, cd_frozen)
                if 0.0 <= (now - chop_until) <= 60.0:
                    return max(0.0, cd_cold)
        except Exception:
            pass

        # M15フリップ直後はCDを短縮
        try:
            last_flip = float(getattr(self, "_last_pivot_flip_ts", 0.0))
            
            # Use Profile-defined CD (55s, 295s, 895s)
            flip_win = float(self.profile.cd_sec)
            
            if last_flip > 0 and (now - last_flip) <= flip_win:
                return max(0.0, base_default * float(self.pivot_flip_cd_factor))
        except Exception:
            pass
        return max(0.0, cd_frozen)


    # StopGridTrader クラス内に追加
    def _update_internal_clock(self) -> float:
        """実時間ではなく、ティックが来ている（市場が動いている）時間だけを進める内部時計。"""
        now_real = time.time()
        diff = max(0.0, min(5.0, now_real - self._last_tick_real_ts))
        self._internal_trading_time += diff
        self._last_tick_real_ts = now_real
        return self._internal_trading_time

    def _save_budget_history(self, force: bool = False) -> None:
        """現在の内部時刻と履歴を相対時間として保存する。"""
        now = time.time()
        if not force and (now - self._last_budget_save_ts < 30.0):
             return
        self._last_budget_save_ts = now
        
        try:
            it = self._internal_trading_time
            def to_rel(lst): return [float(it - t) for t in lst]
            data = {
                "it": it,
                "ehb": to_rel(self._entry_hist_buy),
                "ehs": to_rel(self._entry_hist_sell),
                "chb": to_rel(self._close_hist_buy),
                "chs": to_rel(self._close_hist_sell),
                "ts": now_real if (now_real := getattr(self, "_last_tick_real_ts", 0)) > 0 else now
            }
            with open(self.budget_state_file, "w") as f:
                json.dump(data, f)
        except Exception:
            pass

    def _load_budget_history(self) -> None:
        """JSONからロードし、現在の内部時刻を基準に復元する。"""
        if not os.path.exists(self.budget_state_file):
            return
        try:
            with open(self.budget_state_file, "r") as f:
                data = json.load(f)
            
            self._internal_trading_time = float(data.get("it", 0.0))
            it = self._internal_trading_time
            
            def from_rel(lst): return [float(it - r) for r in lst]
            self._entry_hist_buy = from_rel(data.get("ehb", []))
            self._entry_hist_sell = from_rel(data.get("ehs", []))
            self._close_hist_buy = from_rel(data.get("chb", []))
            self._close_hist_sell = from_rel(data.get("chs", []))
            
            # ロード直後にパージ
            win = float(ENTRY_BUDGET_WINDOW_SEC)
            self._entry_hist_buy[:] = [t for t in self._entry_hist_buy if (it - t) <= win]
            self._entry_hist_sell[:] = [t for t in self._entry_hist_sell if (it - t) <= win]
            self._close_hist_buy[:] = [t for t in self._close_hist_buy if (it - t) <= win]
            self._close_hist_sell[:] = [t for t in self._close_hist_sell if (it - t) <= win]
        except Exception:
            pass

    def _note_close_event(self, side: str = "buy") -> None:
        """ポジションがクローズされた（Budget用）。内部時刻を使用。"""
        try:
            it = self._update_internal_clock()
            s_lower = str(side).lower()
            target_hist = self._close_hist_buy if s_lower == "buy" else self._close_hist_sell

            dedup_sec = float(getattr(self, "CLOSE_EVENT_DEDUP_SEC", 2.0))
            if len(target_hist) > 0 and (it - target_hist[-1]) < dedup_sec:
                 pass 
            else:
                 target_hist.append(float(it))
            
            win = float(ENTRY_BUDGET_WINDOW_SEC)
            target_hist[:] = [t for t in target_hist if (it - t) <= win]
            
            other_hist = self._close_hist_sell if s_lower == "buy" else self._close_hist_buy
            other_hist[:] = [t for t in other_hist if (it - t) <= win]
            
            self._save_budget_history()
        except Exception:
            pass

    def _note_entry_event(self, count: int = 1, side: str = "buy") -> None:
        """エントリーを entry budget に反映。内部時刻を使用。"""
        try:
            n = max(1, int(count))
            it = self._update_internal_clock()
            s_lower = str(side).lower()
            
            target_list = self._entry_hist_buy if s_lower == "buy" else self._entry_hist_sell
            for _ in range(n):
                target_list.append(float(it))

            win = float(ENTRY_BUDGET_WINDOW_SEC)
            target_list[:] = [t for t in target_list if (it - t) <= win]

            self._budget_entry_score = float(getattr(self, "_budget_entry_score", 0.0)) + float(n)
            self._save_budget_history()
        except Exception:
            pass

    # ── anti-chop helpers ─────────────────────────────────────
    def _is_chop_blocked(self) -> bool:
        try:
            return bool(CHOP_GUARD_ENABLE) and (time.time() < float(getattr(self, "_chop_until_ts", 0.0)))
        except Exception:
            return False

    def _cancel_pendings_quiet(self) -> int:
        """GUI/ヘッドレス両対応の未約定削除（ステータスは触らない）。"""
        try:
            # [FIX] Isolation
            orders = self._get_my_orders()
        except Exception:
            orders = []
        n = 0
        for o in orders:
            try:
                if hasattr(mt5, "order_delete"):
                    mt5.order_delete(o.ticket)
                else:
                    self._order_send_with_retry({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket, "symbol": o.symbol})
                n += 1
            except Exception:
                pass
        return n

    def _note_dir_flip(self, tf_name: str, now_ts: float) -> None:
        """方向フリップの履歴を積み、反転多発なら一定時間ブロックする。"""
        if not CHOP_GUARD_ENABLE:
            return
        tf_key = str(tf_name or "").upper()
        if tf_key not in self._flip_hist:
            self._flip_hist[tf_key] = []
        hist = self._flip_hist[tf_key]
        hist.append(float(now_ts))
        win = float(CHOP_FLIP_WINDOW_SEC)
        hist[:] = [t for t in hist if (now_ts - t) <= win]
        if tf_key == str(CHOP_FLIP_TF or "M15").upper():
            if len(hist) >= int(CHOP_FLIP_COUNT):
                until = float(now_ts) + float(CHOP_BLOCK_SEC)
                prev = float(getattr(self, "_chop_until_ts", 0.0))
                self._chop_until_ts = max(prev, until)
                if CHOP_CANCEL_PENDINGS:
                    canceled = self._cancel_pendings_quiet()
                else:
                    canceled = 0
                # ログはスパム防止
                if (now_ts - float(getattr(self, "_last_chop_log_ts", 0.0))) >= 5.0:
                    self._last_chop_log_ts = float(now_ts)
                    try:
                        self._log(
                            f"[CHOP] flips={len(hist)}/{int(CHOP_FLIP_COUNT)} in {int(win)}s → block {int(CHOP_BLOCK_SEC)}s (pend_cancel={canceled})",
                            level=1,
                        )
                    except Exception:
                        pass

    # ── ML gate helpers（Pivotのみ） ──────────────────────────
    def _ai_load_model(self) -> bool:
        if not self.ai_enable:
            self.ai_active = False
            self.ai_model = None
            if not self.headless and getattr(self, "_mon_vars", None):
                try:
                    self._safe_set(self._mon_vars["ai_status"], "AI: 無効 (disabled)")
                    self._safe_set(self._mon_vars["ai_prob"], "p: —")
                except Exception:
                    pass
            return False
        if not _HAS_CATBOOST or CatBoostClassifier is None:
            self.ai_active = False
            self.ai_model = None
            self._log("[AI] catboost が無いのでAI無効（pip install catboost）", tag="AI", level=1)
            if not self.headless and getattr(self, "_mon_vars", None):
                try:
                    self._safe_set(self._mon_vars["ai_status"], "AI: CatBoost未検出 (no catboost)")
                    self._safe_set(self._mon_vars["ai_prob"], "p: —")
                except Exception:
                    pass
            return False
        path = str(self.ai_model_path or "")
        if not path or not os.path.exists(path):
            self.ai_active = False
            self.ai_model = None
            self._log(f"[AI] モデル未検出: {path}（AI無効）", tag="AI", level=1)
            if not self.headless and getattr(self, "_mon_vars", None):
                try:
                    self._safe_set(self._mon_vars["ai_status"], "AI: モデル欠損 (model missing)")
                    self._safe_set(self._mon_vars["ai_prob"], "p: —")
                except Exception:
                    pass
            return False
        try:
            m = CatBoostClassifier()
            m.load_model(path)
            self.ai_model = m
            self.ai_active = True
            self._log(f"[AI] loaded: {path} (thr={self.ai_threshold:.2f})", tag="AI", level=1)
            if not self.headless and getattr(self, "_mon_vars", None):
                try:
                    self._safe_set(self._mon_vars["ai_status"], f"AI: 有効 ({os.path.basename(path)})")
                    self._safe_set(self._mon_vars["ai_prob"], f"thr={self.ai_threshold:.2f}")
                except Exception:
                    pass
            return True
        except Exception as e:
            self.ai_active = False
            self.ai_model = None
            self._log(f"[AI] load error: {e}", tag="AI", level=1)
            if not self.headless and getattr(self, "_mon_vars", None):
                try:
                    self._safe_set(self._mon_vars["ai_status"], "AI: 読込エラー (load error)")
                    self._safe_set(self._mon_vars["ai_prob"], "p: —")
                except Exception:
                    pass
            return False

    def _ai_feature_snapshot(
        self,
        *,
        side: str,
        d1: int,
        d5: int,
        d15: int,
        dH1: int,
        r1: bool,
        r5: bool,
        r15: bool,
        is_m5_both: bool,
        is_m15_both: bool,
        m15_rh: float | None,
        m15_rl: float | None,
        origin: str,
    ) -> dict:
        cache_key = (self._current_bar_ts("M1"), str(side), str(origin))
        if self._ai_cache_key == cache_key and isinstance(self._ai_cache_feat, dict):
            return dict(self._ai_cache_feat)

        tick = mt5.symbol_info_tick(self.symbol)
        info = mt5.symbol_info(self.symbol)
        pt = float(getattr(info, "point", 0.0) or 0.0)
        spread = 0.0
        if tick and pt > 0:
            spread = float(tick.ask - tick.bid)

        rates = self._get_rates("M1", n=15)
        vol_range_abs = 0.0
        vol_range_pct = 0.0
        vol_sum_abs_ret = 0.0
        hour = 0
        if rates:
            highs = [float(r["high"]) for r in rates]
            lows = [float(r["low"]) for r in rates]
            closes = [float(r["close"]) for r in rates]
            vol_range_abs = max(highs) - min(lows)
            last_close = float(closes[-1]) if closes else 0.0
            if last_close != 0.0:
                vol_range_pct = vol_range_abs / abs(last_close)
            for i in range(1, len(closes)):
                prev = float(closes[i - 1])
                cur = float(closes[i])
                if prev != 0.0:
                    vol_sum_abs_ret += abs(cur - prev) / abs(prev)
            try:
                hour = int(time.gmtime(int(rates[-1]["time"])).tm_hour)
            except Exception:
                hour = 0

        # M15: 実体抜け（確定足ベース）
        m15_body_dir = 0
        m15_body_break_up = 0
        m15_body_break_dn = 0
        m15_body_size_pct = 0.0
        try:
            rr = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_M15, 0, 2)
            if rr is not None and len(rr) >= 2:
                prev = rr[1]
                o = float(prev["open"])
                c = float(prev["close"])
                if c > o:
                    m15_body_dir = 1
                elif c < o:
                    m15_body_dir = -1
                if c != 0.0:
                    m15_body_size_pct = abs(c - o) / abs(c)
                rh = float(m15_rh) if m15_rh is not None else None
                rl = float(m15_rl) if m15_rl is not None else None
                if rh is not None and (o <= rh) and (c > rh):
                    m15_body_break_up = 1
                if rl is not None and (o >= rl) and (c < rl):
                    m15_body_break_dn = 1
        except Exception:
            pass

        # ── 新機能: M1 Breakout Logic ──
        # M1 最新確定足のCloseが、その1つ前のHigh/Lowを超えているか
        is_m1_breakout = 0
        if rates and len(rates) >= 2:
             curr = rates[-1]
             prev = rates[-2]
             c_now = float(curr["close"])
             h_prev= float(prev["high"])
             l_prev= float(prev["low"])
             s_val = 1 if side == "buy" else -1
             
             if s_val == 1 and c_now > h_prev: is_m1_breakout = 1
             elif s_val == -1 and c_now < l_prev: is_m1_breakout = 1

        # H1 Match
        h1_match = 1 if (dH1 > 0 and side == "buy") or (dH1 < 0 and side == "sell") else 0
        
        # Consistent Volatility (Avg Range)
        if rates:
             ranges = [float(r["high"])-float(r["low"]) for r in rates]
             vol_avg = sum(ranges) / len(ranges)
        else:
             vol_avg = 0.0

        # Session Flags (Simple Hour Based)
        # Sydney: 22-7 (UTC+0? No, depend on server. Usually MT5 is UTC+2/3)
        # Tokyo: 0-9
        # London: 8-17
        # NY: 13-22
        # Using server hour directly for simplicity as "Session Feature"
        s_tokyo = 1 if (0 <= hour < 9) else 0
        s_london = 1 if (8 <= hour < 17) else 0
        s_ny = 1 if (13 <= hour < 22) else 0
        s_sydney = 1 if (21 <= hour <= 23 or 0 <= hour < 6) else 0

        side_val = 1.0 if side == "buy" else -1.0
        feat = {
            # 新: CatBoostのフォールバック列と揃える
            "vol_range_abs": float(vol_range_abs),
            "vol_range_pct": float(vol_range_pct),
            "vol_sum_abs_ret": float(vol_sum_abs_ret),
            "spread":   float(spread),
            "hour":     int(hour),
            "m1":       float(d1),
            "m5":       float(d5),
            "m15":      float(d15),
            "h1":       float(dH1),
            "r1":       float(r1),
            "r5":       float(r5),
            "r15":      float(r15),
            "m5_both":  float(is_m5_both),
            "m15_both": float(is_m15_both),
            "m15_body_dir": float(m15_body_dir),
            "m15_body_break_up": float(m15_body_break_up),
            "m15_body_break_dn": float(m15_body_break_dn),
            "m15_body_size_pct": float(m15_body_size_pct),
            "side":     float(side_val),
            # 互換: 既存ログ/表示用のキー
            "is_m1_breakout": float(is_m1_breakout),
            "h1_match": float(h1_match),
            "vol_avg":  float(vol_avg),
            "session_tokyo": int(s_tokyo),
            "session_london": int(s_london),
            "session_ny": int(s_ny),
            "session_sydney": int(s_sydney),
            "m15_body": float(m15_body_dir),
            "m15_break_up": float(m15_body_break_up),
            "m15_break_dn": float(m15_body_break_dn),
        }
        self._ai_cache_key = cache_key
        self._ai_cache_feat = dict(feat)
        return feat

    def _ai_predict_prob(self, feat: dict) -> float | None:
        if not self.ai_active or self.ai_model is None:
            return None
        try:
            names = []
            try:
                names = list(getattr(self.ai_model, "feature_names_", []) or [])
            except Exception:
                names = []
            if names:
                x = [[float(feat.get(n, 0.0)) for n in names]]
            else:
                x = [[
                    float(feat.get("vol_range_abs", 0.0)),
                    float(feat.get("vol_range_pct", 0.0)),
                    float(feat.get("vol_sum_abs_ret", 0.0)),
                    float(feat.get("spread", 0.0)),
                    float(feat.get("hour", 0)),
                    float(feat.get("m1", 0)),
                    float(feat.get("m5", 0)),
                    float(feat.get("m15", 0)),
                    float(feat.get("h1", 0)),
                    float(feat.get("r1", 0)),
                    float(feat.get("r5", 0)),
                    float(feat.get("r15", 0)),
                    float(feat.get("m5_both", 0)),
                    float(feat.get("m15_both", 0)),
                    float(feat.get("m15_body_dir", 0)),
                    float(feat.get("m15_body_break_up", 0)),
                    float(feat.get("m15_body_break_dn", 0)),
                    float(feat.get("m15_body_size_pct", 0.0)),
                    float(feat.get("side", 0)),
                ]]
            proba = self.ai_model.predict_proba(x)[0]
            return float(proba[1]) if len(proba) > 1 else float(proba[0])
        except Exception as e:
            if self.ai_debug:
                self._log(f"[AI] predict error: {e}", tag="AI", level=1)
            return None

    def _ai_log_row(self, feat: dict, prob: float | None, passed: bool, context: dict) -> None:
        if not self.ai_log_enable:
            return
        row = dict(feat)
        now_ts = int(time.time())
        row.update({
            "ts": now_ts,
            "session": getattr(self, "_session_id", ""),
            "symbol": self.symbol,
            "origin": context.get("origin", ""),
            "protective": int(bool(context.get("protective", False))),
            "closes_win": int(context.get("closes_win", 0) or 0),
            "entries_win": int(context.get("entries_win", 0) or 0),
            "entry_limit": int(context.get("entry_limit", 0) or 0),
            "ai_apply": int(bool(context.get("ai_apply", False))),
            "passed": int(bool(passed)),
            "prob": "" if prob is None else float(prob),
            "threshold": float(self.ai_threshold),
            "price": float(context.get("price", 0.0) or 0.0),
        })
        path = str(self.ai_log_file or "anyabot_ai_log.csv")
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            exists = os.path.exists(path)
            with open(path, "a", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(row.keys()))
                if not exists:
                    w.writeheader()
                w.writerow(row)
        except Exception:
            pass

    def send_notify(self, title: str, profit: float, positions: list, reason: str, extra_msg: str = ""):
        """Discord通知 (Embed) を非同期送信"""
        if not bool(getattr(self, "_discord_enable_flag", True)):
            return
        url = getattr(self, "discord_url", "")
        if not url: return

        # Gather Context Data
        try:
            acc = mt5.account_info()
            bal = acc.balance if acc else 0.0
            equity = acc.equity if acc else 0.0
        except:
            bal = equity = 0.0
        
        # Drawdown %
        dd_pct = 0.0
        if bal > 0:
            dd_pct = (bal - equity) / bal * 100.0
        
        # Term Progress
        def _get_mon_float(key):
            if not getattr(self, "_mon_vars", None): return 0.0
            val = self._mon_vars.get(key)
            if not val: return 0.0
            s = val.get()
            try: return float(s)
            except: return 0.0

        term_cur = _get_mon_float("term_cur")
        term_tgt = _get_mon_float("term_target")

        def _worker():
            try:
                # Color (Green/Red/Grey)
                color = 0x00ff00 if profit > 0 else (0xff0000 if profit < 0 else 0x95a5a6)
                
                # JST Timestamp
                import datetime
                now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
                ts_str = now_jst.strftime('%Y-%m-%d %H:%M:%S (JST)')

                # Build Fields
                fields = [
                    {"name": "💰 Profit", "value": f"**{profit:+.2f} USD**", "inline": True},
                    {"name": "📉 Drawdown", "value": f"**{dd_pct:.2f}%**", "inline": True},
                    {"name": "📊 Term Prog", "value": f"{term_cur:.2f} / {term_tgt:.2f}", "inline": True},
                    {"name": "📦 Positions", "value": f"{len(positions)}", "inline": True},
                    {"name": "📝 Reason", "value": f"{reason}", "inline": True},
                ]
                if extra_msg:
                    fields.append({"name": "Note", "value": extra_msg, "inline": False})

                embed = {
                    "title": f"{title} {('🚀' if profit>0 else '💀')}",
                    "description": f"Executed at {ts_str}",
                    "color": color,
                    "fields": fields,
                    "footer": {"text": f"Bal: {bal:.2f} | Eq: {equity:.2f}"}
                }
                
                payload = {
                    "username": "Anyabot Grid",
                    "embeds": [embed]
                }
                import requests
                import threading
                requests.post(url, json=payload, timeout=5)
            except Exception as e:
                self._log(f"[NOTIFY ERROR] {e}", level=1)
        
        import threading
        threading.Thread(target=_worker, daemon=True).start()

    def _state_log_row(self) -> None:
        if not self.state_log_enable:
            return
        now = time.time()
        if (now - float(getattr(self, "_last_state_log_ts", 0.0))) < float(self.state_log_interval_sec):
            return
        self._last_state_log_ts = float(now)
        try:
            snap = self._acct_snapshot()
            # [FIX] Isolation
            poss = self._get_my_positions()
            buy_n = sum(1 for p in poss if int(getattr(p, "type", -1)) == int(mt5.POSITION_TYPE_BUY))
            sell_n = sum(1 for p in poss if int(getattr(p, "type", -1)) == int(mt5.POSITION_TYPE_SELL))
            row = {
                "ts": int(now),
                "session": getattr(self, "_session_id", ""),
                "symbol": self.symbol,
                "balance": float(snap.get("balance", 0.0)),
                "equity": float(snap.get("equity", 0.0)),
                "dd_pct": float(snap.get("dd_pct", 0.0)),
                "spread_pts": int(snap.get("spread_pts", 0) or 0),
                "pos_n": int(snap.get("pos_n", 0) or 0),
                "pos_buy": int(buy_n),
                "pos_sell": int(sell_n),
                "pending_n": int(snap.get("pending_n", 0) or 0),
                "tot_vol": float(snap.get("tot_vol", 0.0)),
            }
            path = str(self.state_log_file or "anyabot_state_log.csv")
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            exists = os.path.exists(path)
            with open(path, "a", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(row.keys()))
                if not exists:
                    w.writeheader()
                w.writerow(row)
        except Exception:
            pass

    def _is_minority_rescue_applicable(self, side: str, poss: list) -> bool:
        """Minority Rescue (Effective Count < 2) が適用されるか判定"""
        try:
            buy_n = sum(1 for p in poss if int(getattr(p, "type", -1)) == int(mt5.POSITION_TYPE_BUY))
            sell_n = sum(1 for p in poss if int(getattr(p, "type", -1)) == int(mt5.POSITION_TYPE_SELL))

            # 1. Minority Check
            if buy_n == sell_n or (buy_n + sell_n) < 2:
                return False
            
            minority_side = "buy" if buy_n < sell_n else "sell"
            if str(side).lower() != minority_side:
                return False

            # 2. Effective Count Check (Step*10 distance)
            s_pts = self._compute_step_pts()
            info = mt5.symbol_info(self.symbol); pt = info.point
            dist_limit = s_pts * pt * 10.0
            
            tick = mt5.symbol_info_tick(self.symbol)
            if not tick: return False
            now_price = (tick.bid + tick.ask) / 2.0
            
            target_type = mt5.POSITION_TYPE_BUY if str(side).lower() == "buy" else mt5.POSITION_TYPE_SELL
            eff_cnt = 0
            for p in poss:
                if p.type == target_type:
                    if abs(p.price_open - now_price) <= dist_limit:
                        eff_cnt += 1
            
            return (eff_cnt < 2)
        except Exception:
            return False

    def _entry_budget_check(self, side: str) -> tuple[bool, dict]:
        """
        決済回転率ベースの「自然な」エントリー制限（Buy/Sell個別管理）。
        - 直近window内の決済が増えるほど許可枠が増える
        - buy/sellが偏っている場合、少数側（カバー側）は許可（任意）
        """
        now = self._update_internal_clock()
        
        # ★v10.3: Offset Disable Bypass Logic
        # Offsetが無効化されている場合、Turnoverロジックをスキップして簡易制限に切り替える
        if not bool(getattr(self, "_offset_enable_flag", True)):
            try:
                # [FIX] Isolation
                poss = self._get_cached_positions()
                limit_pos = int(self.max_total_positions)
                
                # 1. Position Count Hard Limit
                if len(poss) >= limit_pos:
                    return False, {"reason": f"MaxPos({len(poss)}/{limit_pos})"}

                # 1.5 Imbalance Guard is only meaningful when both sides exist
                buy_n = sum(1 for p in poss if int(getattr(p, "type", -1)) == int(mt5.POSITION_TYPE_BUY))
                sell_n = sum(1 for p in poss if int(getattr(p, "type", -1)) == int(mt5.POSITION_TYPE_SELL))
                if buy_n == 0 or sell_n == 0:
                    return True, {"reason": "Bypass(single-side)", "protective": False, "bypass": True}
                
                # 2. PnL Imbalance Ratio Check (Relative)
                if bool(globals().get("OFFSET_DISABLE_IMBAL_GUARD_ENABLE", True)):
                    pnl_b = sum(p.profit for p in poss if p.type == mt5.POSITION_TYPE_BUY)
                    pnl_s = sum(p.profit for p in poss if p.type == mt5.POSITION_TYPE_SELL)
                    
                    denom = abs(pnl_b) + abs(pnl_s)
                    ratio = 0.0
                    if denom > 0.001:
                        ratio = abs(pnl_b - pnl_s) / denom
                    
                    # 閾値チェック (例: 0.6)
                    if ratio > float(OFFSET_DISABLE_IMBAL_RATIO):
                        # 自分が「負けている側」ならブロック
                        my_pnl = pnl_b if str(side).lower() == "buy" else pnl_s
                        op_pnl = pnl_s if str(side).lower() == "buy" else pnl_b
                        
                        if my_pnl < op_pnl:
                            return False, {"reason": f"ImbalRatio({ratio:.2f}>{OFFSET_DISABLE_IMBAL_RATIO})", "protective": False}

                return True, {"entries_win": 0, "closes_win": 0, "limit": limit_pos, "protective": False, "bypass": True}
            except Exception as e:
                self._log(f"[BUDGET] Bypass check error: {e}", level=2)
                return False, {}

        if not ENTRY_BUDGET_ENABLE:
            return True, {"entries_win": 0, "closes_win": 0, "limit": 0, "protective": False}

        # 予算リセット (Budget Reset)
        # ノーポジになったら履歴をクリアして、次のサイクルをBaseから開始させる
        mt5_err = False
        try:
            magic = int(getattr(self, "magic", getattr(self, "InpMagic", 0)))
            # [FIX] Isolation
            r_poss = self._get_cached_positions()
            if r_poss is None:
                mt5_err = True  # ★v10.3 Fix: 取得失敗時はリセットしない
                r_poss = []
            
            # Filter by Magic
            poss = []
            for p in r_poss:
                if magic == 0 or int(getattr(p, "magic", 0)) == magic:
                    poss.append(p)
            pos_n = len(poss)
        except Exception:
            mt5_err = True
            pos_n = 0
            
        if pos_n == 0 and not mt5_err:
            # ★v10.2: ノーポジ時は相殺状態をリセット
            # ★v10.3 Fix: 起動直後は猶予を与える（MT5接続安定化のため）
            startup_grace_sec = 10.0
            if hasattr(self, "_bot_start_ts") and (time.time() - self._bot_start_ts) < startup_grace_sec:
                pass  # 起動直後はスキップ
            elif self._offset_state.get("active", False) or self._offset_state.get("realized_profit", 0.0) > 0:
                self._log("[BUDGET] Zero positions detected. Resetting offset state.", level=1)
                self._reset_offset_state()


        # Soft Close Block
        if getattr(self, "market_closing_block", False):
            return False, {"entries_win": 0, "closes_win": 0, "limit": 0, "protective": False, "reason": "MarketClosing"}

        win = float(ENTRY_BUDGET_WINDOW_SEC)
        it = self._internal_trading_time
        
        s_lower = str(side).lower()
        if s_lower == "buy":
            self._entry_hist_buy[:] = [t for t in self._entry_hist_buy if (it - t) <= win]
            self._close_hist_buy[:] = [t for t in self._close_hist_buy if (it - t) <= win]
            entries_win = len(self._entry_hist_buy)
            closes_win = len(self._close_hist_buy)
        else:
            self._entry_hist_sell[:] = [t for t in self._entry_hist_sell if (it - t) <= win]
            self._close_hist_sell[:] = [t for t in self._close_hist_sell if (it - t) <= win]
            entries_win = len(self._entry_hist_sell)
            closes_win = len(self._close_hist_sell)


        
        # Per-Side Bypass Check (New Logic: 片側2本未満なら許可)
        side_pos_by_type = sum(1 for p in poss if str(getattr(p, "type", -1)) == str(mt5.POSITION_TYPE_BUY if s_lower == "buy" else mt5.POSITION_TYPE_SELL))
        if side_pos_by_type < int(ENTRY_BUDGET_BYPASS_UNDER_POS):
             return True, {"entries_win": entries_win, "closes_win": closes_win, "limit": -1, "protective": True}

        limit = int(ENTRY_BUDGET_BASE) + int(ENTRY_BUDGET_PER_CLOSE) * int(closes_win)
        limit = max(0, min(int(ENTRY_BUDGET_CAP), int(limit)))
        
        # ★v10.2: 回転低下時の制限強化
        # closes=0（完全停滞）時は最低限のみ許可
        if closes_win == 0 and entries_win >= 2:
            limit = max(1, int(ENTRY_BUDGET_BASE) // 2)
            # self._log(f"[BUDGET] Zero closes restriction: limit={limit}", level=2)
        elif TURNOVER_RATE_ENABLE and entries_win > 0:
            # 回転率 = closes / entries が低い → limit減
            turnover_rate = float(closes_win) / float(entries_win)
            if turnover_rate < float(TURNOVER_RATE_MIN):
                limit = max(1, limit - int(TURNOVER_RATE_PENALTY))
                # self._log(f"[TURNOVER] Low rate {turnover_rate:.2f} -> limit reduced", level=2)
        
        # [DEBUG]
        if entries_win >= limit:
             # Reduce log frequency (One per 60s)
             now_ts = time.time()
             if now_ts - getattr(self, "_last_budget_deny_log", 0) > 60:
                 self._log(f"[BUDGET DENY DEBUG] pos_n={pos_n} side_pos={side_pos_by_type} entries={entries_win} limit={limit} side={side} (Throttled)", level=1)
                 self._last_budget_deny_log = now_ts
        limit = max(0, min(int(ENTRY_BUDGET_CAP), int(limit)))

        protective = False
        if ENTRY_BUDGET_BYPASS_MINOR:
            try:
                # Count-based minority check
                buy_n = sum(1 for p in poss if int(getattr(p, "type", -1)) == int(mt5.POSITION_TYPE_BUY))
                sell_n = sum(1 for p in poss if int(getattr(p, "type", -1)) == int(mt5.POSITION_TYPE_SELL))
                if buy_n != sell_n and (buy_n + sell_n) >= 2:
                    minority = "buy" if buy_n < sell_n else "sell"
                    if str(side).lower() == minority:
                        protective = True
                
                # Rescue Check (Limit=999) using shared logic
                if self._is_minority_rescue_applicable(side, poss):
                    # Safety Cap Check
                    target_side_cnt = sum(1 for p in poss if str(getattr(p, "type", -1)) == str(mt5.POSITION_TYPE_BUY if str(side).lower() == "buy" else mt5.POSITION_TYPE_SELL))
                    if target_side_cnt < int(RESCUE_ENTRY_CAP):
                        protective = True
                        limit = 999 
                    else:
                        # Cap Reached - No Rescue
                        pass 

            except Exception:
                pass

        # ★追加: トレンド方向ボーナス (H1順張りなら枠拡張)
        try:
            h1_dir, *_ = self._tf_dir("H1")
            req_dir = 1 if str(side).lower() == "buy" else -1
            if h1_dir == req_dir:
                limit += int(ENTRY_BUDGET_TREND_BONUS)
        except Exception:
            pass

        # ★変更: Protectiveも無限ではなくボーナス枠加算方式にする
        if protective:
            limit += int(PROTECTIVE_MODE_BONUS)
            # protectiveフラグはUI表示用に残すが、判定はlimit比較で行う

        ok = entries_win < limit
        if not self.headless and getattr(self, "_mon_vars", None):
            try:
                # Update UI cache and display both
                status_str = f"{'B' if s_lower=='buy' else 'S'}:{entries_win}/{limit}" + ("(prot)" if protective else "")
                self._budget_status_cache[s_lower] = status_str
                # Join for display: "B:1/3 S:2/3(prot) C=..."
                # Note: C and P are snapshot of current check, might be slightly inconsistent but acceptable
                full_display = f"{self._budget_status_cache['buy']} {self._budget_status_cache['sell']} C={closes_win} P={pos_n}"
                self._safe_set(self._mon_vars["entry_budget"], full_display)
            except Exception:
                pass
        
        # protectiveであっても limit を超えたら False になる（エンジン停止 → Recycle発動の契機）
        return ok, {"entries_win": entries_win, "closes_win": closes_win, "limit": limit, "protective": protective}

    def _reset_offset_queue(self) -> None:
        """相殺後の再発注キューをリセット。"""
        try:
            self._offset_retry_queue = []
            self._offset_queue_anchor_side = None
        except Exception:
            pass
    
    def _reset_offset_state(self) -> None:
        """★v10.2: 相殺状態をリセット（完了時またはノーポジ時に呼ぶ）"""
        try:
            self._offset_state = {
                "active": False,
                "closed_tickets": set(),
                "realized_profit": 0.0,
                "winner_count_buy": 0,
                "winner_count_sell": 0,
                "last_boss_side": None,
                "final_winner_side": None,
                "final_winner_count": 0,
                "boss_history": {},  # ★v10.3: Bossごとの進行状況（中断対策）
            }
            self._log("[OFFSET] State reset", level=2)
            
            # ★v10.3: 停滞時間追跡用
            self._last_offset_complete_ts = time.time()
            
            # ★v10.3: JSON永続化
            try:
                import datetime
                import tempfile
                stag_file = os.path.join(os.path.dirname(__file__), f"offset_stag_{self.symbol}.json")
                dir_path = os.path.dirname(stag_file)
                data = {
                    "last_offset_complete_ts": self._last_offset_complete_ts,
                    "updated_at": datetime.datetime.now().isoformat()
                }
                # アトミック書き込み
                with tempfile.NamedTemporaryFile(mode='w', dir=dir_path, suffix='.tmp', delete=False) as tf:
                    json.dump(data, tf)
                    temp_path = tf.name
                try:
                    if os.path.exists(stag_file):
                        os.remove(stag_file)
                except Exception:
                    pass
                os.rename(temp_path, stag_file)
            except Exception as e:
                self._log(f"[OFFSET] Failed to save stagnation file: {e}", level=2)
            
            # ★v10.2: GUI更新
            if not self.headless and getattr(self, "_mon_vars", None):
                try:
                    self._safe_set(self._mon_vars["offset_state"], "—")
                except: pass
        except Exception:
            pass
        # ★v10.3: 初期化時も保存
        self._save_offset_state_to_disk()

    def _save_global_close_state(self) -> None:
        """総利益クローズ進行状態の永続化"""
        try:
            data = self._global_close_state.copy()
            with open(self.global_close_state_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            self._log(f"[GLOBAL-CLOSE] save failed: {e}", level=2)

    def _load_global_close_state(self) -> None:
        """総利益クローズ進行状態の読み込み"""
        try:
            if not os.path.exists(self.global_close_state_file):
                return
            with open(self.global_close_state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._global_close_state.update({
                    "active": bool(data.get("active", False)),
                    "realized_profit": float(data.get("realized_profit", 0.0) or 0.0),
                    "started_ts": float(data.get("started_ts", 0.0) or 0.0),
                })
        except Exception as e:
            self._log(f"[GLOBAL-CLOSE] load failed: {e}", level=2)

    # ─────────────────────────────────────────────────────────
    # ★v10.3 Persistence
    # ─────────────────────────────────────────────────────────
    def _save_offset_state_to_disk(self):
        """Offset状態と停滞時間をJSONに保存（アトミック書き込みで空ファイル防止）"""
        try:
            import datetime
            import tempfile
            # set -> list変換
            state_copy = self._offset_state.copy()
            state_copy["closed_tickets"] = list(state_copy["closed_tickets"])
            
            data = {
                "last_offset_complete_ts": self._last_offset_complete_ts,
                "offset_state": state_copy,
                "offset_enable": bool(getattr(self, "_offset_enable_flag", True)),
                "updated_at": datetime.datetime.now().isoformat()
            }
            
            path = os.path.join(os.path.dirname(__file__), f"offset_state_v10_{self.symbol}.json")
            # アトミック書き込み: 一時ファイルに書いてからリネーム
            dir_path = os.path.dirname(path)
            with tempfile.NamedTemporaryFile(mode='w', dir=dir_path, suffix='.tmp', delete=False) as tf:
                json.dump(data, tf, indent=2)
                temp_path = tf.name
            # Windowsではリネーム前に既存ファイルを削除する必要がある場合がある
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
            os.rename(temp_path, path)
        except Exception as e:
            self._log(f"[WARN] Failed to save offset state: {e}", level=1)

    def _load_offset_state_from_disk(self):
        """起動時にOffset状態を復元"""
        try:
            path = os.path.join(os.path.dirname(__file__), f"offset_state_v10_{self.symbol}.json")
            if not os.path.exists(path):
                # 旧stagファイルの読み込み試行 (Migration)
                stag_path = os.path.join(os.path.dirname(__file__), f"offset_stag_{self.symbol}.json")
                if os.path.exists(stag_path):
                     with open(stag_path, "r") as f:
                        d = json.load(f)
                        self._last_offset_complete_ts = d.get("last_offset_complete_ts", time.time())
                return

            with open(path, "r") as f:
                data = json.load(f)
                
            self._last_offset_complete_ts = data.get("last_offset_complete_ts", time.time())
            
            saved_state = data.get("offset_state", {})
            if saved_state:
                # Type conversion
                if "closed_tickets" in saved_state:
                    saved_state["closed_tickets"] = set(saved_state["closed_tickets"])
                
                # boss_history keys (str -> int)
                if "boss_history" in saved_state:
                    bh = saved_state["boss_history"]
                    saved_state["boss_history"] = {int(k): v for k, v in bh.items()}
                
                # Merge into current default state
                for k, v in saved_state.items():
                    self._offset_state[k] = v
            
            # Restore Offset Enable
            if "offset_enable" in data:
                val = bool(data["offset_enable"])
                self._loaded_offset_enable = val # Store for late-init GUI
                self._offset_enable_flag = val
                if getattr(self, "offset_enable_var", None):
                    self.offset_enable_var.set(val)
                    
            self._log(f"[OFFSET] State restored. Profit=${self._offset_state['realized_profit']:.1f}, Enable={data.get('offset_enable', True)}", level=1)
            
        except Exception as e:
            self._log(f"[WARN] Failed to load offset state: {e}", level=1)

    # ─────────────────────────────────────────────────────────
    # Pivot Strict Skip C1 Persistence
    # ─────────────────────────────────────────────────────────
    def _save_pivot_config(self) -> None:
        """Pivot設定の永続化（v10.6拡張: 4新オプション含む）"""
        try:
            data = {
                "pivot_strict_skip_c1": bool(getattr(self, "pivot_strict_skip_c1", PIVOT_STRICT_SKIP_C1)),
                "pivot_first_require_ref2_body_break": bool(getattr(self, "pivot_first_require_ref2_body_break", PIVOT_FIRST_REQUIRE_REF2_BODY_BREAK)),
                "pivot_first_require_upper_body_break": bool(getattr(self, "pivot_first_require_upper_body_break", PIVOT_FIRST_REQUIRE_UPPER_BODY_BREAK)),
                "pivot_zigzag_entry_enable": bool(getattr(self, "pivot_zigzag_entry_enable", PIVOT_ZIGZAG_ENTRY_ENABLE)),
                "pivot_first_entry_relax_enable": bool(getattr(self, "pivot_first_entry_relax_enable", PIVOT_FIRST_ENTRY_RELAX_ENABLE)),
                "pivot_first_body_break_only_enable": bool(getattr(self, "pivot_first_body_break_only_enable", PIVOT_FIRST_BODY_BREAK_ONLY_ENABLE)),
                "m1_pairnet_enable": bool(getattr(self, "m1_pairnet_enable", M1_PAIRNET_ENABLE)),
                "nanpin_full_hedge_enable": bool(getattr(self, "nanpin_full_hedge_enable", NANPIN_FULL_HEDGE_ENABLE)),
                "nanpin_prevent_enable": bool(getattr(self, "nanpin_prevent_enable", NANPIN_PREVENT_ENABLE)),
            }
            path = getattr(self, "pivot_config_file", os.path.join(os.path.abspath(os.path.dirname(__file__) or "."), f"pivot_config_{self.symbol}.json"))
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._log(f"[WARN] Failed to save pivot config: {e}", level=1)

    def _load_pivot_config(self) -> None:
        """Pivot設定の読み込み（v10.6拡張: 4新オプション含む）"""
        try:
            path = getattr(self, "pivot_config_file", os.path.join(os.path.abspath(os.path.dirname(__file__) or "."), f"pivot_config_{self.symbol}.json"))
            if not os.path.exists(path):
                return
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            if "pivot_strict_skip_c1" in data:
                self.pivot_strict_skip_c1 = bool(data["pivot_strict_skip_c1"])
            if "pivot_first_require_ref2_body_break" in data:
                self.pivot_first_require_ref2_body_break = bool(data["pivot_first_require_ref2_body_break"])
            if "pivot_first_require_upper_body_break" in data:
                self.pivot_first_require_upper_body_break = bool(data["pivot_first_require_upper_body_break"])
            if "pivot_zigzag_entry_enable" in data:
                self.pivot_zigzag_entry_enable = bool(data["pivot_zigzag_entry_enable"])
            if "pivot_first_entry_relax_enable" in data:
                self.pivot_first_entry_relax_enable = bool(data["pivot_first_entry_relax_enable"])
            if "pivot_first_body_break_only_enable" in data:
                self.pivot_first_body_break_only_enable = bool(data["pivot_first_body_break_only_enable"])
            if "m1_pairnet_enable" in data:
                self.m1_pairnet_enable = bool(data["m1_pairnet_enable"])
            if "nanpin_full_hedge_enable" in data:
                self.nanpin_full_hedge_enable = bool(data["nanpin_full_hedge_enable"])
            if "nanpin_prevent_enable" in data:
                self.nanpin_prevent_enable = bool(data["nanpin_prevent_enable"])
        except Exception as e:
            self._log(f"[WARN] Failed to load pivot config: {e}", level=1)

    # ── v10.6: Nanpin Prevention Guard (ナンピン防止) ──────────────────
    def _is_nanpin_prevented(self, side: str) -> bool:
        """ナンピン防止ガード:
        多数側が負け・少数側が勝ちなら多数側エントリーを禁止。
        改善版: ボリュームベース + swap込みPnL + 両側にポジション必須。
        Returns True if entry should be BLOCKED.
        """
        if not self.nanpin_prevent_enable:
            return False
        try:
            poss = self._get_cached_positions()
            if not poss:
                return False

            buy_poss = [p for p in poss if p.type == mt5.POSITION_TYPE_BUY]
            sell_poss = [p for p in poss if p.type == mt5.POSITION_TYPE_SELL]

            # 両側にポジションがないと発動しない
            if not buy_poss or not sell_poss:
                return False

            buy_vol = round(sum(p.volume for p in buy_poss), 2)
            sell_vol = round(sum(p.volume for p in sell_poss), 2)
            # swap込みの実質損益
            buy_pnl = sum(p.profit + getattr(p, "swap", 0.0) for p in buy_poss)
            sell_pnl = sum(p.profit + getattr(p, "swap", 0.0) for p in sell_poss)

            blocked = False
            if side == "buy":
                # BUYブロック: BUY側vol > SELL側 AND BUY側PnL < 0 AND SELL側PnL > 0
                if buy_vol > sell_vol and buy_pnl < 0 and sell_pnl > 0:
                    blocked = True
            elif side == "sell":
                # SELLブロック: SELL側vol > BUY側 AND SELL側PnL < 0 AND BUY側PnL > 0
                if sell_vol > buy_vol and sell_pnl < 0 and buy_pnl > 0:
                    blocked = True

            if blocked:
                self._log(
                    f"[NP-GUARD] {side.upper()} BLOCKED: "
                    f"BUY({buy_vol:.2f}lots ${buy_pnl:.2f}) "
                    f"SELL({sell_vol:.2f}lots ${sell_pnl:.2f})",
                    level=1,
                )
            return blocked
        except Exception:
            return False

    def _calc_majority_dynamic_params(self, positions: list) -> tuple:
        """
        市場状態（トレンド強度・偏り比率・含み損深度）に基づき、
        MAJORITY_NANPIN_FILTER_MIN_DIFF と MAJORITY_PROFIT_LOCK_TOP_N を動的補正する。

        Returns:
            (eff_nanpin_diff, eff_lock_n)
        スコア例 (base_diff=2, base_lock_n=2):
            強トレンド一致・健全   → diff=4, lock=3
            ニュートラル           → diff=2, lock=2  (or 1)
            逆行・高偏り・深損     → diff=1(min), lock=0
        """
        base_diff   = int(MAJORITY_NANPIN_FILTER_MIN_DIFF)
        base_lock_n = int(MAJORITY_PROFIT_LOCK_TOP_N)

        if not MAJORITY_DYNAMIC_ENABLE:
            return base_diff, base_lock_n

        # 1. 多数派サイドの特定
        n_buy  = sum(1 for p in positions if p.type == mt5.POSITION_TYPE_BUY)
        n_sell = sum(1 for p in positions if p.type == mt5.POSITION_TYPE_SELL)
        maj_type = mt5.POSITION_TYPE_BUY if n_buy >= n_sell else mt5.POSITION_TYPE_SELL
        maj_str  = "buy" if maj_type == mt5.POSITION_TYPE_BUY else "sell"
        imbal    = abs(n_buy - n_sell)

        # 2. トレンド一致スコア (M1/M5/M15/H1, 範囲: −4 〜 +4)
        #    正: 多数派方向と一致するTF多い, 負: 逆行TF多い
        trend_align = 0
        for tf in ("M1", "M5", "M15", "H1"):
            try:
                d, *_ = self._tf_dir(tf)
                if d == 0:
                    continue
                if (maj_str == "buy" and d > 0) or (maj_str == "sell" and d < 0):
                    trend_align += 1
                else:
                    trend_align -= 1
            except Exception:
                pass

        # 3. 多数派含み損 (0以上: 多数派が赤字のドル額)
        maj_poss = [p for p in positions if p.type == maj_type]
        maj_pnl  = sum(p.profit for p in maj_poss)
        maj_loss = max(0.0, -maj_pnl)

        # ── eff_nanpin_diff の補正 ──────────────────────────────
        # トレンド一致強 → 緩和(+) / 逆行強 → 厳格化(−)
        # 偏り大 → 厳格化(−)
        # 含み損深 → 厳格化(−)
        nanpin_adj = 0
        if   trend_align >=  int(MAJORITY_TREND_STRONG_TH): nanpin_adj += 2
        elif trend_align >=  int(MAJORITY_TREND_WEAK_TH):   nanpin_adj += 1
        elif trend_align <= -int(MAJORITY_TREND_STRONG_TH): nanpin_adj -= 2
        elif trend_align <= -int(MAJORITY_TREND_WEAK_TH):   nanpin_adj -= 1

        if   imbal >= int(MAJORITY_IMBAL_HIGH_TH): nanpin_adj -= 2
        elif imbal >= int(MAJORITY_IMBAL_MID_TH):  nanpin_adj -= 1

        if   maj_loss >= float(MAJORITY_LOSS_DEEP_USD): nanpin_adj -= 2
        elif maj_loss >= float(MAJORITY_LOSS_MID_USD):  nanpin_adj -= 1

        eff_nanpin_diff = max(1, base_diff + nanpin_adj)

        # ── eff_lock_n の補正 ───────────────────────────────────
        # トレンド一致強 → 利益を守る → ロック多く(+)
        # 偏り大かつ利益あり → ロック多く(+)
        # 含み損深 → ロック対象が減る → ロック少なく(−)
        lock_adj = 0
        if   trend_align >=  int(MAJORITY_TREND_STRONG_TH): lock_adj += 1
        elif trend_align <= -int(MAJORITY_TREND_STRONG_TH): lock_adj -= 1

        if   imbal >= int(MAJORITY_IMBAL_HIGH_TH): lock_adj += 1
        elif imbal <= 2:                            lock_adj -= 1

        if   maj_loss >= float(MAJORITY_LOSS_DEEP_USD): lock_adj -= 2
        elif maj_loss >= float(MAJORITY_LOSS_MID_USD):  lock_adj -= 1

        eff_lock_n = max(0, base_lock_n + lock_adj)

        self._log(
            f"[MAJ-DYN] {maj_str.upper()} imbal={imbal} trend={trend_align:+d} "
            f"loss={maj_loss:.1f} | diff {base_diff}→{eff_nanpin_diff} lock_n {base_lock_n}→{eff_lock_n}",
            level=2,
        )
        return eff_nanpin_diff, eff_lock_n

    def _is_majority_nanpin_filtered(self, side: str) -> bool:
        """
        多数派ナンピンフィルター（カウントベース）:
        buy/sell のポジション数差が MAJORITY_NANPIN_FILTER_MIN_DIFF 以上の場合、
        多数派サイドへの新規エントリーをブロック。少数派サイドは許可（ヘッジ・カバー用）。
        既存の _is_nanpin_prevented（PnLベース）とは独立して動作。
        Returns True if entry should be BLOCKED.
        """
        if not MAJORITY_NANPIN_FILTER_ENABLE:
            return False
        try:
            poss = self._get_cached_positions()
            if not poss:
                return False

            n_buy  = sum(1 for p in poss if p.type == mt5.POSITION_TYPE_BUY)
            n_sell = sum(1 for p in poss if p.type == mt5.POSITION_TYPE_SELL)
            # 動的パラメータ取得（MAJORITY_DYNAMIC_ENABLE=False なら静的値）
            diff, _ = self._calc_majority_dynamic_params(poss)
            side_l = str(side).lower()

            blocked = False
            if side_l == "buy" and (n_buy - n_sell) >= diff:
                # 多数派(BUY)が含み損 = ナンピン → ブロック
                # 多数派(BUY)が含み益 = ピラミッディング → 許可
                buy_pnl = sum(p.profit for p in poss if p.type == mt5.POSITION_TYPE_BUY)
                if buy_pnl < 0:
                    blocked = True
            elif side_l == "sell" and (n_sell - n_buy) >= diff:
                sell_pnl = sum(p.profit for p in poss if p.type == mt5.POSITION_TYPE_SELL)
                if sell_pnl < 0:
                    blocked = True

            if blocked:
                self._log(
                    f"[MAJ-NP] {side_l.upper()} BLOCKED (nanpin): buy={n_buy}, sell={n_sell} (diff>={diff})",
                    level=1,
                )
            return blocked
        except Exception:
            return False

    # ── v10.6: Nanpin Hedge (段階的比率方式) ──────────────────
    def _calc_hedge_ratio(self, dominant_vol: float) -> tuple:
        """段階的ヘッジ比率を計算。
        Returns: (hedge_ratio, target_net)
        """
        vol_ratio = dominant_vol / max(self.lot, 0.01)
        hedge_ratio = NANPIN_HEDGE_MAX_RATIO
        for threshold, ratio in NANPIN_HEDGE_TIERS:
            if vol_ratio <= threshold:
                hedge_ratio = ratio
                break
        target_net = round(dominant_vol * (1.0 - hedge_ratio), 2)
        return hedge_ratio, target_net

    def _check_nanpin_hedge(self) -> None:
        """M1確定時にヘッジ条件をチェックし、条件合致でヘッジ発注。
        条件: ① nanpin_lock ② 全決済中でない ③ net露出>target_net ④ M1 body_break ⑤ spread ⑥ cooldown
        """
        if not self.nanpin_full_hedge_enable:
            return
        # ① nanpinモード中のみ
        if not self._nanpin_lock:
            return
        # ② 全決済処理中はヘッジしない
        if getattr(self, "_closing_in_progress", False):
            return
        try:
            poss = self._get_cached_positions()
            if not poss:
                return

            buy_vol = sum(p.volume for p in poss if p.type == mt5.POSITION_TYPE_BUY)
            sell_vol = sum(p.volume for p in poss if p.type == mt5.POSITION_TYPE_SELL)

            if buy_vol > sell_vol:
                dominant_side = "buy"
                hedge_side = "sell"
                net_exposure = round(buy_vol - sell_vol, 2)
                dominant_vol = buy_vol
            elif sell_vol > buy_vol:
                dominant_side = "sell"
                hedge_side = "buy"
                net_exposure = round(sell_vol - buy_vol, 2)
                dominant_vol = sell_vol
            else:
                self._update_hedge_gui()
                return  # 完全バランス

            # ③ 段階的比率でターゲットnet計算
            hedge_ratio, target_net = self._calc_hedge_ratio(dominant_vol)
            # ★ 常にGUI更新 (条件④-⑥の結果に関わらず最新表示)
            self._update_hedge_gui(
                net_exposure=net_exposure,
                target_net=target_net,
                hedge_ratio=hedge_ratio,
            )
            # ③-b ステートレス判定: ライブポジションから必要ヘッジ量を算出
            # 実効ターゲット: 比率ベース OR 最低1本ヘッジ の厳しい方
            effective_target = min(target_net, round(dominant_vol - self.lot, 2))

            if net_exposure <= effective_target:
                return  # 既に十分ヘッジされている

            # 差分を切り上げ（ロット単位）
            hedge_vol_raw = net_exposure - effective_target
            hedge_lots = math.ceil(hedge_vol_raw / self.lot)
            hedge_vol = round(hedge_lots * self.lot, 2)
            if hedge_vol <= 0:
                return

            # ④ M1 body_break がヘッジ方向と一致
            _, _, _, _, bb_m1 = self._tf_dir("M1")
            if dominant_side == "buy" and bb_m1 != -1:
                return  # BUY保有だがM1が下抜けしてない → 見送り
            if dominant_side == "sell" and bb_m1 != 1:
                return  # SELL保有だがM1が上抜けしてない → 見送り

            # ⑤ スプレッドチェック
            tick = mt5.symbol_info_tick(self.symbol)
            if not tick:
                return
            info = mt5.symbol_info(self.symbol)
            pt = (info.point if info else 0.01) or 0.01
            spread_pts = (tick.ask - tick.bid) / pt
            if spread_pts > self.spread_max_pts:
                return  # スプレッド過大 → 見送り

            # ⑥ クールダウンチェック
            now = time.time()
            if (now - self._nanpin_hedge_last_ts) < NANPIN_HEDGE_COOLDOWN_SEC:
                return  # クールダウン中

            price = tick.ask if hedge_side == "buy" else tick.bid
            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": self.symbol,
                "volume": hedge_vol,
                "type": mt5.ORDER_TYPE_BUY if hedge_side == "buy" else mt5.ORDER_TYPE_SELL,
                "price": price,
                "deviation": DEVIATION,
                "magic": self.magic,
                "comment": "nanpin-hedge",
                "origin": "nanpin_hedge",
            }
            ok = self._order_send_with_retry(req)
            if ok:
                self._nanpin_hedge_done = True
                self._nanpin_hedge_vol = round(self._nanpin_hedge_vol + hedge_vol, 2)
                self._nanpin_hedge_last_ts = now
                new_net = round(net_exposure - hedge_vol, 2)
                vol_ratio = round(dominant_vol / max(self.lot, 0.01), 1)
                self._log(
                    f"[HEDGE] {hedge_side.upper()} +{hedge_vol} lots @ {price:.5f} "
                    f"(net:{net_exposure:.2f}→{new_net:.2f}, ratio:{hedge_ratio*100:.0f}%, "
                    f"vol:{dominant_vol:.2f}={vol_ratio:.1f}x, total_hedge:{self._nanpin_hedge_vol:.2f})",
                    level=1,
                )
                self._update_hedge_gui(
                    net_exposure=new_net,
                    target_net=target_net,
                    hedge_ratio=hedge_ratio,
                    )
            else:
                self._log(f"[HEDGE] Order FAILED: {hedge_side} {hedge_vol}", level=1)
        except Exception as e:
            self._log(f"[HEDGE] Check error: {e}", level=2)

    def _update_hedge_gui(
        self,
        net_exposure: float = 0.0,
        target_net: float = 0.0,
        hedge_ratio: float = 0.0,
    ) -> None:
        """GUI上のヘッジ状態表示を更新 (段階的比率方式)"""
        if self._nanpin_hedge_vol > 0:
            txt = f"Hedge:{self._nanpin_hedge_vol:.2f}lots (net:{net_exposure:.2f}/tgt:{target_net:.2f} {hedge_ratio*100:.0f}%)"
        elif self._nanpin_lock and self.nanpin_full_hedge_enable:
            if target_net > 0:
                txt = f"Hedge:WAIT (net:{net_exposure:.2f}/tgt:{target_net:.2f} {hedge_ratio*100:.0f}%)"
            else:
                txt = f"Hedge:WAIT (net:{net_exposure:.2f})"
        else:
            txt = "Hedge:—"
        self._safe_set(self._mon_vars.get("hedge_state"), txt)

    def _refresh_hedge_gui(self) -> None:
        """ヘッジGUI表示を毎tick更新 (軽量版: net/target_net/ratioを再計算して表示のみ)"""
        if not self.nanpin_full_hedge_enable:
            return
        if not self._nanpin_lock:
            self._update_hedge_gui()
            return
        try:
            poss = self._get_cached_positions()
            if not poss:
                self._update_hedge_gui()
                return
            buy_vol = sum(p.volume for p in poss if p.type == mt5.POSITION_TYPE_BUY)
            sell_vol = sum(p.volume for p in poss if p.type == mt5.POSITION_TYPE_SELL)
            net_exposure = round(abs(buy_vol - sell_vol), 2)
            dominant_vol = max(buy_vol, sell_vol)
            hedge_ratio, target_net = self._calc_hedge_ratio(dominant_vol)
            self._update_hedge_gui(
                net_exposure=net_exposure,
                target_net=target_net,
                hedge_ratio=hedge_ratio,
            )
        except Exception:
            pass

    def _refresh_np_guard_gui(self) -> None:
        """NP Guard GUI表示を毎tick更新 (軽量版)"""
        if not self.nanpin_prevent_enable:
            self._safe_set(self._mon_vars.get("np_guard_state"), "NP:—")
            return
        try:
            poss = self._get_cached_positions()
            if not poss:
                self._safe_set(self._mon_vars.get("np_guard_state"), "NP:OK")
                return
            buy_poss = [p for p in poss if p.type == mt5.POSITION_TYPE_BUY]
            sell_poss = [p for p in poss if p.type == mt5.POSITION_TYPE_SELL]
            if not buy_poss or not sell_poss:
                self._safe_set(self._mon_vars.get("np_guard_state"), "NP:OK")
                return
            buy_vol = round(sum(p.volume for p in buy_poss), 2)
            sell_vol = round(sum(p.volume for p in sell_poss), 2)
            buy_pnl = sum(p.profit + getattr(p, "swap", 0.0) for p in buy_poss)
            sell_pnl = sum(p.profit + getattr(p, "swap", 0.0) for p in sell_poss)
            buy_blocked = (buy_vol > sell_vol and buy_pnl < 0 and sell_pnl > 0)
            sell_blocked = (sell_vol > buy_vol and sell_pnl < 0 and buy_pnl > 0)
            if buy_blocked:
                txt = f"NP:BUY禁止 B({buy_vol:.2f}/${buy_pnl:.1f}) S({sell_vol:.2f}/${sell_pnl:.1f})"
            elif sell_blocked:
                txt = f"NP:SELL禁止 B({buy_vol:.2f}/${buy_pnl:.1f}) S({sell_vol:.2f}/${sell_pnl:.1f})"
            else:
                txt = f"NP:OK B({buy_vol:.2f}/${buy_pnl:.1f}) S({sell_vol:.2f}/${sell_pnl:.1f})"
            self._safe_set(self._mon_vars.get("np_guard_state"), txt)
        except Exception:
            pass

    # ── v10.6: Zigzag Entry Permission State Update ──────────────
    def _update_zigzag_state(self) -> None:
        """exec_tfのスイングピボットを計算し、エントリー許可フラグを更新"""
        if not self.pivot_zigzag_entry_enable:
            return
        try:
            # ref2方向取得
            tf_ref2 = self._get_tf_str(self.profile.ref2_tf)
            d_ref2, _, _, _, _ = self._tf_dir(tf_ref2)

            # c2 body break取得
            tf_c2 = self._get_tf_str(self.profile.c2_tf)
            _, _, _, _, bb_c2 = self._tf_dir(tf_c2)

            # exec_tf: 確定バーを取得してスイングピボット計算
            bars = PIVOT_ZIGZAG_BARS
            with _MT5_LOCK:
                rates = mt5.copy_rates_from_pos(self.symbol, self.profile.exec_tf, 1, bars)
            if rates is None or len(rates) < 10:
                return
            his = rates[:-1]
            last_bar = rates[-1]
            target_close = float(last_bar['close'])

            highs  = [float(r['high'])  for r in his]
            lows   = [float(r['low'])   for r in his]
            opens  = [float(r['open'])  for r in his]
            closes = [float(r['close']) for r in his]

            _, _, _, _, sh, sl = _adjdir_from_series_with_pivots(
                highs, lows, opens, closes, new_count=True
            )
            if sh is not None:
                self._zz_swing_high = sh
            if sl is not None:
                self._zz_swing_low = sl

            sh_disp = f"{self._zz_swing_high:.2f}" if self._zz_swing_high else "—"
            sl_disp = f"{self._zz_swing_low:.2f}" if self._zz_swing_low else "—"

            # === BUY方向 (ref2 UP) ===
            if d_ref2 > 0:
                self._zz_c2_clear = (bb_c2 != -1)
                if not self._zz_c2_clear:
                    if self._zz_entry_flag:
                        self._log("[ZZ] FLAG OFF (C2 bearish break)", tag="ZZ", level=1)
                    self._zz_entry_flag = False
                else:
                    if self._zz_swing_high is not None and target_close > self._zz_swing_high:
                        if not self._zz_entry_flag:
                            self._log(f"[ZZ] FLAG ON BUY (close {target_close:.5f} > SH {self._zz_swing_high:.5f})", tag="ZZ", level=1)
                        self._zz_entry_flag = True
                    if self._zz_swing_low is not None and target_close < self._zz_swing_low:
                        if self._zz_entry_flag:
                            self._log(f"[ZZ] FLAG OFF (close {target_close:.5f} < SL {self._zz_swing_low:.5f})", tag="ZZ", level=1)
                        self._zz_entry_flag = False

            # === SELL方向 (ref2 DOWN) ===
            elif d_ref2 < 0:
                self._zz_c2_clear = (bb_c2 != 1)
                if not self._zz_c2_clear:
                    if self._zz_entry_flag:
                        self._log("[ZZ] FLAG OFF (C2 bullish break)", tag="ZZ", level=1)
                    self._zz_entry_flag = False
                else:
                    if self._zz_swing_low is not None and target_close < self._zz_swing_low:
                        if not self._zz_entry_flag:
                            self._log(f"[ZZ] FLAG ON SELL (close {target_close:.5f} < SL {self._zz_swing_low:.5f})", tag="ZZ", level=1)
                        self._zz_entry_flag = True
                    if self._zz_swing_high is not None and target_close > self._zz_swing_high:
                        if self._zz_entry_flag:
                            self._log(f"[ZZ] FLAG OFF (close {target_close:.5f} > SH {self._zz_swing_high:.5f})", tag="ZZ", level=1)
                        self._zz_entry_flag = False
            else:
                self._zz_entry_flag = False

            # GUI更新
            if self._zz_entry_flag:
                zz_txt = f"ZZ:ON H:{sh_disp} L:{sl_disp}"
            elif not self._zz_c2_clear:
                zz_txt = f"ZZ:WAIT(C2) H:{sh_disp} L:{sl_disp}"
            else:
                zz_txt = f"ZZ:OFF H:{sh_disp} L:{sl_disp}"
            try:
                self._safe_set(self._mon_vars.get("zz_state"), zz_txt)
            except Exception:
                pass

        except Exception as e:
            self._log(f"[ZZ] update error: {e}", tag="ZZ", level=2)

    # ### PATCH: term base setter (guarded)
    def _set_term_base(self, new_base: float, why: str) -> None:
        """明示タイミングでのみ term 基準を更新し、ログを残す"""
        try:
            old = self._term_base
            self._term_base = float(new_base)
            self._term_start_ts = time.time()
            self._term_last_roll_ts = 0.0
            self._log(f"[TERM] base set ({why}): {old if old is not None else 'None'} -> {self._term_base:.2f}", tag="TERM", level=1)
            self._reset_offset_queue()
            self._refresh_ui()
            self._save_term_state()
        except Exception as e:
            self._log(f"[TERM] base set error: {e}", tag="TERM", level=2)

    def _save_term_state(self) -> None:
        """term基準を外部ファイルに保存して引き継ぎ可にする。"""
        try:
            acc = mt5.account_info()
            state = {
                "base": self._term_base,
                "start_ts": self._term_start_ts,
                "last_roll_ts": self._term_last_roll_ts,
                "use_equity": bool(self.term_use_equity),
                "symbol": self.symbol,
                "login": int(acc.login) if acc and acc.login is not None else None,
                "ts": time.time(),
            }
            with open(self.term_state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._log(f"[TERM] save state error: {e}", tag="TERM", level=2)

    def _load_term_state(self) -> bool:
        """保存されたterm基準を読み込む。適合しない場合はFalse。"""
        try:
            if not os.path.isfile(self.term_state_file):
                return False
            with open(self.term_state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            acc = mt5.account_info()
            login = int(acc.login) if acc and acc.login is not None else None
            if state.get("symbol") != self.symbol:
                return False
            if state.get("login") not in (None, login):
                return False
            base = state.get("base")
            if base is None:
                return False
            self._term_base = float(base)
            self._term_start_ts = float(state.get("start_ts", time.time()))
            self._term_last_roll_ts = float(state.get("last_roll_ts", 0.0))
            self._log(f"[TERM] loaded state: base={self._term_base:.2f} login={login}", tag="TERM", level=1)
            return True
        except Exception as e:
            self._log(f"[TERM] load state error: {e}", tag="TERM", level=2)
            return False

    def _set_term_state_path(self, login: int | None) -> None:
        """term_state_file を login + symbol で固有化し、競合を避ける。"""
        try:
            base_dir = os.path.dirname(self.term_state_file) or os.path.dirname(__file__) or "."
            os.makedirs(base_dir, exist_ok=True)
            login_part = str(login) if login is not None else "anon"
            sym_part = (self.symbol or "SYMBOL").replace("/", "_")
            self.term_state_file = os.path.join(base_dir, f"term_state_{login_part}_{sym_part}.json")
        except Exception as e:
            self._log(f"[TERM] set path error: {e}", tag="TERM", level=2)

    def _ignore_external_cashflow_if_needed(self, cur_bal: float, cur_eq: float, n_pos: int) -> None:
        """
        入金/出金などポジション非依存の残高変化が term 進捗に影響しないよう base を調整。
        片側ポジション以下のときのみ反映。ただしゼロポジ時は調整しない（他通貨の影響を無視）。
        """
        try:
            if n_pos <= 0:
                return
            last = cur_eq if self.term_use_equity else cur_bal
            prev = self._term_last_eq_snap if self.term_use_equity else self._term_last_bal_snap
            if prev is None:
                return
            delta = last - prev
            if n_pos <= 1:
                thresh = max(1.0, self.term_step_usd * 0.5)
                if abs(delta) >= thresh:
                    self._term_base = float(self._term_base or last) + delta
                    self._log(f"[TERM] adjust base for cashflow delta={delta:.2f} -> base={self._term_base:.2f}", tag="TERM", level=1)
        except Exception as e:
            self._log(f"[TERM] cashflow adjust error: {e}", tag="TERM", level=2)

            return 0
    
    # [NEW] Helper for Realized PnL (Isolation)
    def _get_my_realized_pnl(self, start_ts: float) -> float:
        """Calculate realized PnL since start_ts for this Magic Number"""
        try:
            if start_ts <= 0: return 0.0
            ignore = bool(getattr(self, "_ignore_magic_flag", False))

            dt_from = datetime.datetime.fromtimestamp(start_ts)
            deals = mt5.history_deals_get(date_from=dt_from, date_to=datetime.datetime.now() + datetime.timedelta(seconds=60))
            if deals is None: return 0.0

            magic = int(getattr(self, "magic", 0))
            total = 0.0
            for d in deals:
                # Filter by Magic
                if not ignore and magic != 0:
                    if int(getattr(d, "magic", 0)) != magic:
                        continue
                # Sum result
                total += float(getattr(d, "profit", 0.0)) + float(getattr(d, "swap", 0.0)) + float(getattr(d, "commission", 0.0))
            return total
        except Exception:
            return 0.0

    def _check_term_rollover_and_close(self) -> int:
        if not getattr(self, "term_enable", True):
            return 0

        # 基準が未設定のときのみ、現在値で初期化（初回だけ）
        if self._term_base is None:
            # [FIX] Use isolated helper
            poss_init = self._get_my_positions()
            if len(poss_init) >= 2:
                snap = self._acct_snapshot()
                cur  = float(snap["equity"] if self.term_use_equity else snap["balance"])
                self._set_term_base(cur, why="lazy-init")
                try:
                    self._term_last_bal_snap = float(snap["balance"])
                    self._term_last_eq_snap = float(snap["equity"])
                except Exception:
                    pass
            return 0

        poss = self._get_my_positions()
        snap_now = self._acct_snapshot()
        self._ignore_external_cashflow_if_needed(
            cur_bal=float(snap_now["balance"]),
            cur_eq=float(snap_now["equity"]),
            n_pos=len(poss),
        )

        # ==== ここが元の原因ポイント ====
        # [削除前]
        # if not poss:
        #     snap = self._acct_snapshot()
        #     self._term_base = float(snap["equity"] if self.term_use_equity else snap["balance"])
        #     self._term_start_ts = time.time()
        #     return 0
        # [修正後] → フラグで明示的に許可された場合だけ
        if not poss:
            if getattr(self, "TERM_RESET_BASE_WHEN_EMPTY", TERM_RESET_BASE_WHEN_EMPTY):
                snap = self._acct_snapshot()
                cur  = float(snap["equity"] if self.term_use_equity else snap["balance"])
                self._set_term_base(cur, why="empty-positions")
                try:
                    self._term_last_bal_snap = float(snap["balance"])
                    self._term_last_eq_snap = float(snap["equity"])
                except Exception:
                    pass
            return 0

        now = time.time()
        if (now - self._term_last_roll_ts) < self.term_cooldown_sec:
            return 0
        if (now - self._term_start_ts) < self.term_min_hold_sec:
            return 0

        if (now - self._term_start_ts) < self.term_min_hold_sec:
            return 0

        # [FIX] PnL Isolation Logic
        progressed = 0.0
        ignore_magic = bool(getattr(self, "_ignore_magic_flag", False))

        if ignore_magic:
            # Legacy Global Mode (Account Balance)
            snap = self._acct_snapshot()
            cur  = float(snap["equity"] if self.term_use_equity else snap["balance"])
            progressed = cur - float(self._term_base)
        else:
            # Isolated Mode (Instance PnL)
            # Progress = Realized (since term start) + Floating
            realized = self._get_my_realized_pnl(self._term_start_ts)
            poss = self._get_my_positions()
            floating = sum(float(getattr(p, "profit", 0.0)) + float(getattr(p, "swap", 0.0)) for p in poss)
            progressed = realized + floating

        if progressed >= self.term_step_usd:
            roll_floor = getattr(self, "term_rollover_floor", None)
            roll_min = -1e12 if roll_floor is None else float(roll_floor)
            # ★v10: PreserveスキップをGUI設定から反映
            skip_p = bool(getattr(self, "_preserve_toggle_flag", PROFIT_PRESERVE_ENABLE))
            n, completed = self._close_all_positions_with_floor(roll_min, skip_preserve=skip_p)
            self._log(f"[TERM] rollover: cur={cur:.2f} base={self._term_base:.2f} "
                    f"+{progressed:.2f}>=step {self.term_step_usd:.2f} -> closed {n}", tag="TERM", level=1)
            # === Discord通知追加 ===
            # === Discord通知追加 ===
            self.send_notify(
                title="Term Rollover Complete",
                profit=progressed,
                positions=[],
                reason=f"Rollover (Base -> {cur:.2f})",
                extra_msg=f"Closed {n} positions. New Base: {cur:.2f}"
            )
            if completed:
                # [FIX] Isolation
                poss_after = self._get_my_positions()
                if not poss_after:
                    snap_after = self._acct_snapshot()
                    cur_after = float(snap_after["equity"] if self.term_use_equity else snap_after["balance"])
                    self._set_term_base(cur_after, why="rollover")
            return n

        if self.term_allow_step_down and progressed <= -self.term_step_usd:
            self._log(f"[TERM] step-down: cur={cur:.2f} base={self._term_base:.2f}", tag="TERM", level=1)
            self._set_term_base(cur, why="step-down")

        try:
            self._term_last_bal_snap = float(snap["balance"])
            self._term_last_eq_snap = float(snap["equity"])
        except Exception:
            pass
        return 0


# ### PATCH: safe close-all
    def _close_all_positions_safely(self) -> int:
        """
        close-by 優先の全決済。未対応/失敗は成行フォールバック。
        逆行して利益フロアを割る場合は中断し、ストラテジーを継続。
        """
        min_profit = float(getattr(self, "close_min_profit_floor", 0.0))
        try:
            if self.term_close_use_closeby and getattr(self, "use_close_by", True):
                if hasattr(self, "_close_all_positions"):
                    closed, completed = self._close_all_positions_with_floor(min_profit)
                    if completed:
                        self._note_close_event()
                        self.send_notify(
                            title="All Positions Closed",
                            profit=0.0,
                            positions=[],
                            reason="Close-All (Safe)",
                            extra_msg=f"Closed {closed} positions ({self.symbol})"
                        )
                    else:
                        self._set_status(f"Close aborted (PnL floor {min_profit:.2f})")
                    return closed
        except Exception:
            pass

        n = 0
        acc_realized = 0.0
        
        # [FIX] Freeze Prevention: Timeout & Loop Limit
        start_ts = time.time()
        timeout_sec = 30.0
        max_loops = 50
        loop_cnt = 0
        
        while True:
            loop_cnt += 1
            if loop_cnt > max_loops or (time.time() - start_ts) > timeout_sec:
               self._log(f"[CLOSE] Safely-close aborted: Timeout/MaxLoop (n={loop_cnt})", level=1)
               break
            
            # [FIX] Isolation
            poss = self._get_my_positions()
            if not poss:
                self._note_close_event()
                self.send_notify(
                    title="All Positions Closed",
                    profit=0.0,
                    positions=[],
                    reason="Close-All (Empty)",
                    extra_msg=f"Checked {n} positions (Already Empty)"
                )
                return n

            current_unrealized = sum(float(getattr(p, "profit", 0.0) or 0.0) for p in poss)
            total_net = current_unrealized + acc_realized
            if total_net < min_profit:
                self._log(f"[CLOSE] abort: Net PnL {total_net:.2f} (Unr:{current_unrealized:.2f}+Rel:{acc_realized:.2f}) < floor {min_profit:.2f}", tag="CLOSE", level=1)
                self._set_status(f"Close aborted (Net {total_net:.2f} < floor {min_profit:.2f})")
                return n

            p = poss[0]
            t = mt5.symbol_info_tick(self.symbol)
            if not t:
                return n
            req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": self.symbol,
                   "volume": float(p.volume), "deviation": DEVIATION, "magic": self.magic,
                   "comment": "term-roll"}
            if p.type == mt5.POSITION_TYPE_BUY:
                req["type"] = mt5.ORDER_TYPE_SELL; req["price"] = float(t.bid)
            else:
                req["type"] = mt5.ORDER_TYPE_BUY;  req["price"] = float(t.ask)
            if self._order_send_with_retry(req):
                n += 1
                acc_realized += float(getattr(p, "profit", 0.0))
            
            time.sleep(0.5) # [FIX] CPU yield


# ### PATCH: term ui & daemon
    def _refresh_term_ui_from_values(self, bal: float, eq: float) -> None:
        """Term表示を更新（GUIのみ）。"""
        if self.headless or not getattr(self, "_mon_vars", None):
            return
        try:
            base = float(self._term_base) if (self._term_base is not None) else (eq if self.term_use_equity else bal)
            cur  = float(eq if self.term_use_equity else bal)
            step = float(self.term_step_usd)
            prog = cur - base
            self._safe_set(self._mon_vars["term_base"], f"{base:.2f} ({'EQ' if self.term_use_equity else 'BAL'})")
            self._safe_set(self._mon_vars["term_target"], f"+{step:.2f}")
            self._safe_set(self._mon_vars["term_cur"], f"{cur:.2f}")
            self._safe_set(self._mon_vars["term_prog"], f"{prog:+.2f}/{step:.2f}")
        except Exception as e:
            self._log(f"[MON] term ui error: {e}", tag="MON", level=2)
    def _refresh_pivot_heat_ui(self) -> None:
        """PivotのCDゾーンと動的CD、直近決済からの経過秒をGUIに表示"""
        if self.headless or not getattr(self, "_mon_vars", None):
            return
        try:
            import time

            # 動的CD廃止 -> 固定CDを表示
            dyn_cd = float(getattr(self, "pivot_cooldown_sec", 10.0))

            # 直近決済のタイムスタンプ（※ _note_close_event 側と揃える）
            last_ts = float(getattr(self, "_last_close_event_ts", 0.0)) or 0.0
            now = time.time()
            ago = max(0.0, now - last_ts) if last_ts > 0.0 else 0.0

            # _pivot_dynamic_cd と同じ閾値を使ってゾーン名を決定
            hot_sec  = float(getattr(self, "pivot_hot_sec", 10.0))
            warm_sec = float(getattr(self, "pivot_warm_sec", 5 * 60.0))
            cold_sec = float(getattr(self, "pivot_cold_sec", 15 * 60.0))

            if last_ts <= 0.0:
                zone = "待機中 ⌛"   # まだ決済なし
            elif ago <= hot_sec:
                zone = "激アツ 🔥"
            elif ago <= warm_sec:
                zone = "安定 🙂"
            elif ago <= cold_sec:
                zone = "静観 🧊"
            else:
                zone = "沈黙 ❄️"

            self._safe_set(self._mon_vars["pivot_heat"], zone)
            self._safe_set(self._mon_vars["pivot_cd"], f"{dyn_cd:.2f}s")
            self._safe_set(
                self._mon_vars["last_close_ago"],
                "—" if last_ts == 0.0 else f"{ago:.0f}s",
            )

        except Exception as e:
            # ログはメインスレッドでなくても基本的に安全
            self._log(f"[MON] pivot heat ui error: {e}", tag="MON", level=2)

    def _mark_offset_block(self) -> None:
        """相殺直後にPivotエントリーを一時停止するためのタイムスタンプを更新。"""
        try:
            self._last_offset_block_ts = time.time()
        except Exception:
            self._last_offset_block_ts = 0.0

    def _detect_close_event_from_positions(self) -> None:
        """
        SL/TP/手動決済など、bot経由ではないクローズを検出して Entry Budget に反映する。
        - positions の ticket セットが減ったら「決済が起きた」とみなす
        - ticketごとに type(方向) を覚えておき、消えた ticket の方向を _note_close_event(side) に渡す
        """
        try:
            # [FIX] Isolation
            poss = self._get_cached_positions()
            # 現在の {ticket: type} マップを作成
            cur_tracker = {int(p.ticket): int(p.type) for p in poss}
            
            # 初回起動時はトラッカーを作るだけ
            prev_tracker = getattr(self, "_pos_tracker", None)
            if prev_tracker is None:
                self._pos_tracker = cur_tracker
                return

            # 前回あって今回ない ticket を探す
            closed_tickets = set(prev_tracker.keys()) - set(cur_tracker.keys())
            
            if closed_tickets:
                for t in closed_tickets:
                    # 方向を特定
                    p_type = prev_tracker.get(t)
                    side_str = "buy" # default
                    if p_type is not None:
                        if p_type == mt5.POSITION_TYPE_SELL:
                            side_str = "sell"
                        elif p_type == mt5.POSITION_TYPE_BUY:
                            side_str = "buy"
                    
                    # 記録
                    self._note_close_event(side=side_str)

            # トラッカー更新
            self._pos_tracker = cur_tracker

        except Exception:
            pass


    def _term_daemon(self) -> None:
        """1秒ごとに term を監視・UI更新・必要なら全決済（軽量ループ）。"""
        while True:
            try:
                self._check_term_rollover_and_close()
                # UI だけでも更新
                snap = self._acct_snapshot()
                self._refresh_pivot_heat_ui()
                self._refresh_term_ui_from_values(snap["balance"], snap["equity"])
            except Exception as e:
                self._log(f"[TERM] daemon error: {e}", tag="TERM", level=2)
            time.sleep(1.0)


    def _enforce_account_allowlist(self):
        """許可リストに現在のMT5ログイン番号が含まれているかを検証。"""  # (note)
        try:
            allow = fetch_allowlist_csv()
        except Exception as e:
            msg = (
                "口座リストの取得に失敗しました。\n"  # (note)
                "ネットワーク環境をご確認ください。\n\n"  # (note)
                f"詳細: {e}\n\n"  # (note)
                "アプリを終了します。"  # (note)
            )
            if not self.headless and getattr(self, "root", None):
                try:
                    messagebox.showerror("認証エラー", msg, parent=self.root)  # (note)
                finally:
                    try: mt5.shutdown()
                    except: pass
                    self.root.after(50, self.root.quit)
                    raise SystemExit(1)
            else:
                try: mt5.shutdown()
                except: pass
                raise SystemExit(msg)

        acc = mt5.account_info()
        login = int(acc.login) if acc and acc.login is not None else None
        if (login is None) or (login not in allow):
            msg = (
                "この口座は認証されていません。\n\n"  # (note)
                f"口座番号: {login}\n"  # (note)
                "許可リストに登録が無いか、反映されていない可能性があります。\n\n"  # (note)
                "アプリを終了します。"  # (note)
            )
            if not self.headless and getattr(self, "root", None):
                try:
                    messagebox.showerror("認証エラー", msg, parent=self.root)  # (note)
                finally:
                    try: mt5.shutdown()
                    except: pass
                    self.root.after(50, self.root.quit)
                    raise SystemExit(1)
            else:
                try: mt5.shutdown()
                except: pass
                raise SystemExit(msg)
    # ── MT5 init ─────────────────────────────────────────────
    def _mt5_init(self) -> None:
        if not mt5.initialize(path=self.path if self.path else None):
            c, m = mt5.last_error()
            raise RuntimeError(f"MT5 init: {c} {m}")
        if not mt5.symbol_select(self.symbol, True):
            raise RuntimeError(f"Cannot select symbol {self.symbol}")
        # 口座Allowlist認証
        self._enforce_account_allowlist()
        try:
            snap = self._acct_snapshot()
            bal = float(snap["balance"]); eq = float(snap["equity"])
            self._term_init_balance = bal
            self._term_init_equity  = eq
            acc = mt5.account_info()
            login = int(acc.login) if acc and acc.login is not None else None
            self._set_term_state_path(login)
            self._term_base = None  # lazy-init (>=2ポジ時) or state load
            self._term_start_ts = time.time()
            self._term_last_roll_ts = 0.0
            self._term_last_bal_snap = bal
            self._term_last_eq_snap = eq
            loaded = self._load_term_state()
            if loaded:
                self._log(f"[TERM] session resume with saved base={self._term_base:.2f}", tag="TERM", level=1)
            else:
                self._log(f"[TERM] session start: balance_start={bal:.2f}, equity_start={eq:.2f}", tag="TERM", level=1)
            # GUI にも反映
            if not self.headless and hasattr(self, "_mon_vars"):
                self._safe_set(self._mon_vars["bal_start"], f"{bal:.2f}")
        except Exception as e:
            self._log(f"[TERM] init log failed: {e}", tag="TERM", level=2)

        # ### PATCH: launch lightweight term daemon (1Hz)
        if not self._term_daemon_started:
            self._term_daemon_started = True
            th = threading.Thread(target=self._term_daemon, name="term-daemon", daemon=True)
            th.start()
        # Startup Notification (v10)
        self.send_notify(title="Startup Test", profit=0.0, positions=[], reason="Boot", extra_msg="Anyabot v10 Activated.")



    def _get_inst_tag(self) -> str:
        """プロファイル名に基づいてインスタンス識別タグを返す"""
        prof_name = getattr(self.profile, "name", "") if hasattr(self, "profile") else ""
        if "Scalp" in prof_name: return "[SCA]"
        if "Day" in prof_name: return "[DAY]"
        if "Swing" in prof_name: return "[SWG]"
        return ""

    # ── logging ──────────────────────────────────────────────
    def _log(self, *args, level: int = 1, tag: str = "PAIRNET"):
        tag = str(tag or "PAIRNET").upper()
        min_v = int(TAG_MIN_VERBOSITY.get(tag, 0))
        
        # ★Console出力: 設定依存 / File出力: 常に許可(Level 2含む)
        should_print = LOG_VERBOSITY >= max(level, min_v)
        should_file = True
        
        if not should_print and not should_file:
            return

        # ★v10 Unified Mode Log Prefix: Identify which instance is logging
        inst_tag = self._get_inst_tag()
        msg = f"{inst_tag}[{tag}] " + " ".join(str(a) for a in args)
        now = time.time()
        key = f"__log_{tag}"
        last = getattr(self, key, 0.0)
        
        # ★Debug(Lv2)以上はレートリミット無視（詳細記録のため）
        if level <= 1 and (now - last < LOG_RATE_LIMIT_SEC):
            return
        setattr(self, key, now)

        if should_print:
            print(msg)
        
        if should_file and LOG_TO_FILE:
            try:
                # [FIX] Thread-Safe Logging
                with _LOG_LOCK:
                    if os.path.exists(self.log_file) and os.path.getsize(self.log_file) > LOG_MAX_SIZE_KB * 1024:
                        os.replace(self.log_file, self.log_file + ".1")
                    with open(self.log_file, "a", encoding="utf-8") as f:
                        f.write(msg + "\n")
            except Exception as e:
                print(f"[LOG ERROR] Failed to write to {self.log_file}: {e}")


    # ── filling mode ─────────────────────────────────────────
    def _choose_filling(self) -> int:
        if self._filling is not None:
            return self._filling
        info = mt5.symbol_info(self.symbol)
        mode_flags = getattr(info, "filling_mode", 0)
        
        # [FIX] Interpret bit flags to OrderFilling enum
        # SYMBOL_FILLING_FOK(1), SYMBOL_FILLING_IOC(2)
        # ORDER_FILLING_FOK(0), ORDER_FILLING_IOC(1), ORDER_FILLING_RETURN(2)
        
        # Default fallback
        final_mode = getattr(mt5, "ORDER_FILLING_IOC", 1)

        if isinstance(mode_flags, int) and mode_flags > 0:
            if (mode_flags & 2): # SYMBOL_FILLING_IOC
                final_mode = getattr(mt5, "ORDER_FILLING_IOC", 1)
            elif (mode_flags & 1): # SYMBOL_FILLING_FOK
                final_mode = getattr(mt5, "ORDER_FILLING_FOK", 0)
        
        self._filling = final_mode
        return self._filling

    def _order_send_with_retry(self, req: dict) -> bool:
        req = dict(req)
        origin = req.pop("origin", None)
        action = req.get("action")

        if action in (getattr(mt5, "TRADE_ACTION_DEAL", None), getattr(mt5, "TRADE_ACTION_PENDING", None)):
            req.setdefault("type_time", mt5.ORDER_TIME_GTC)
            req.setdefault("type_filling", self._choose_filling())

        # Auto-prefix comment with Profile Shorthand (v10.4)
        comm = str(req.get("comment", ""))
        pre = self._get_comm_pre(comm)
        if pre:
            if not comm.startswith(f"[{pre}]"):
                req["comment"] = f"[{pre}]{comm}"[:31]
        result = mt5.order_send(req)
        # 既存 _order_send_with_retry の中（result = mt5.order_send(req) の直後あたり）に追記
        self._log(f"[ORD] send action={req.get('action')} type={req.get('type')} "
                f"price={req.get('price')} vol={req.get('volume')} -> ret={getattr(result,'retcode',None)}",
                tag="ORD", level=1)
        try:
            le = mt5.last_error()
            self._log(f"[ORD] last_error={le}", tag="ORD", level=2)
        except Exception:
            pass
        
        if result is None:
            return False

        # 成功コード（必要に応じて拡張）
        OK = {10009, 10008, 10024}  # DONE, PLACED, REQUEST_ADDED など
        BAD_FILL = {10030, 10031}

        # 成行DEALの“直近エントリ価格”メモ（最小距離ガード用）
        try:
            if origin and req.get("action") == mt5.TRADE_ACTION_DEAL:
                bucket = self._origin_alias.get(origin, origin)
                if bucket in self._last_entry_by_origin:
                    t = req.get("type")
                    side = "buy" if t == mt5.ORDER_TYPE_BUY else "sell"
                    self._last_entry_by_origin[bucket][side] = float(req.get("price", 0.0))
        except Exception:
            pass

        # フィリング不一致はモード変更で再送
        if action in (getattr(mt5, "TRADE_ACTION_DEAL", None), getattr(mt5, "TRADE_ACTION_PENDING", None)) and getattr(result, "retcode", 0) in BAD_FILL:
            self._log(_t("log.fill.unsupported", mode=req['type_filling'], code=result.retcode))
            tried = {req["type_filling"]}
            for cand in self._fill_retry_order:
                if cand is None or cand in tried:
                    continue
                req["type_filling"] = cand
                res2 = mt5.order_send(req)
                if res2 and getattr(res2, "retcode", 0) not in BAD_FILL:
                    self._filling = cand
                    self._log(_t("log.fill.switched", mode=cand, code=res2.retcode))
                    return getattr(res2, "retcode", 0) in OK
            self._log(_t("log.fill.failed", action=req.get('action')))
            return False

        return getattr(result, "retcode", 0) in OK

    def _normalize_close_by_tickets(self, ticket_a: int, ticket_b: int) -> tuple[int, int]:
        """Return (buy_ticket, sell_ticket) when possible."""
        try:
            tickets = {int(ticket_a), int(ticket_b)}
            types = {
                int(getattr(p, "ticket", 0)): int(getattr(p, "type", -1))
                for p in self._get_my_positions()
                if int(getattr(p, "ticket", 0)) in tickets
            }
            type_a = types.get(int(ticket_a))
            type_b = types.get(int(ticket_b))
            if type_a == mt5.POSITION_TYPE_BUY and type_b == mt5.POSITION_TYPE_SELL:
                return int(ticket_a), int(ticket_b)
            if type_a == mt5.POSITION_TYPE_SELL and type_b == mt5.POSITION_TYPE_BUY:
                return int(ticket_b), int(ticket_a)
        except Exception:
            pass
        return int(ticket_a), int(ticket_b)

    def _send_close_by(self, ticket_a: int, ticket_b: int, *, comment: str, deviation: int, log_prefix: str) -> bool:
        """Try close_by with normalized buy/sell ordering, then swapped fallback."""
        buy_ticket, sell_ticket = self._normalize_close_by_tickets(ticket_a, ticket_b)
        tried: list[tuple[int, int]] = []
        candidates = [(buy_ticket, sell_ticket), (sell_ticket, buy_ticket)]
        for position_ticket, position_by_ticket in candidates:
            pair = (int(position_ticket), int(position_by_ticket))
            if pair[0] <= 0 or pair[1] <= 0 or pair[0] == pair[1] or pair in tried:
                continue
            tried.append(pair)
            ok = bool(self._order_send_with_retry({
                "action": mt5.TRADE_ACTION_CLOSE_BY,
                "position": pair[0],
                "position_by": pair[1],
                "symbol": self.symbol,
                "deviation": int(deviation),
                "magic": int(getattr(self, "magic", 0)),
                "comment": comment,
            }))
            if ok:
                self._log(
                    f"{log_prefix} close_by success: position={pair[0]} position_by={pair[1]}",
                    level=1,
                )
                return True
            self._log(
                f"{log_prefix} close_by failed: position={pair[0]} position_by={pair[1]}",
                level=1,
            )
        return False



    # ── guards & snapshots ───────────────────────────────────
    def _acct_snapshot(self):
        acc = mt5.account_info()
        bal = getattr(acc, "balance", 0.0) or 0.0
        eq  = getattr(acc, "equity", bal) or bal
        info = mt5.symbol_info(self.symbol)
        tick = mt5.symbol_info_tick(self.symbol)
        pt = info.point if info else 0.0
        spread_pts = int(round(((tick.ask - tick.bid) / pt))) if (tick and pt) else 0
        # [FIX] Isolation
        poss = self._get_my_positions()
        ords = self._get_my_orders()
        tot_vol = sum(p.volume for p in poss)
        dd_pct = max(0.0, (bal - eq) / bal * 100.0) if bal > 0 else 0.0
        # （_acct_snapshot の return 直前の dict 作成後に追記）
        snap_dict = {"balance": bal, "equity": eq, "dd_pct": dd_pct, "spread_pts": spread_pts,
                     "pos_n": len(poss), "pending_n": len(ords), "tot_vol": tot_vol}
        # ### PATCH: refresh Term UI from snapshot (GUI only)
        self._refresh_term_ui_from_values(bal, eq)
        return snap_dict


    def _rate_ok(self, tag: str, limit_per_min: int) -> bool:
        now = time.time()
        hist = self._rate_hist.setdefault(tag, [])
        hist[:] = [t for t in hist if now - t <= 60.0]
        if len(hist) >= limit_per_min: return False
        hist.append(now); return True

    def _recenter_ok(self) -> bool:
        now = time.time()
        self._recenters[:] = [t for t in self._recenters if now - t <= 60.0]
        if len(self._recenters) >= self.max_recenter_per_min: return False
        self._recenters.append(now); return True

    def _risk_allows_new(self) -> tuple[bool, str]:
        try:
            if self._is_chop_blocked():
                remain = float(getattr(self, "_chop_until_ts", 0.0)) - time.time()
                return False, f"CHOP {max(0.0, remain):.0f}s"
        except Exception:
            pass
        # 決済処理中はエントリーをブロック（同一インスタンスのみ）
        if getattr(self, "_closing_in_progress", False):
            reason = getattr(self, "_closing_reason", "") or "closing"
            return False, reason
        snap = self._acct_snapshot()
        if self.hard_stop_equity is not None and snap["equity"] < self.hard_stop_equity:
            return False, f"equity<{self.hard_stop_equity:.2f}"
        if snap["dd_pct"] >= self.dd_stop_pct:
            return False, f"DD {snap['dd_pct']:.1f}%>{self.dd_stop_pct:.1f}%"
        if snap["spread_pts"] >= self.spread_max_pts:
            return False, f"spread {snap['spread_pts']}>{self.spread_max_pts}"
        if snap["pos_n"] >= self.max_positions:
            return False, f"positions {snap['pos_n']}>{self.max_positions}"
        if snap["tot_vol"] >= self.max_volume:
            return False, f"volume {snap['tot_vol']}>{self.max_volume}"
        if snap["pending_n"] >= self.max_pending:
            return False, f"pending {snap['pending_n']}>{self.max_pending}"
        if not self._rate_ok("orders", self.max_orders_per_min):
            return False, f"rate limit {self.max_orders_per_min}/min"
        # Volatility Guard (ATR-based)
        if self.volatility_guard_enable and self._is_high_volatility():
            return False, "volatility spike"
        # Time-based Entry Block
        if self.block_time_enable and self._is_blocked_time():
            return False, "blocked time"
        return True, "ok"

    def _is_high_volatility(self) -> bool:
        """ATRベースのボラティリティスパイク検出。直近ATR/平均ATR >= 倍率ならTrue"""
        try:
            info = mt5.symbol_info(self.symbol)
            if not info or not info.point:
                return False
            pt = info.point
            
            n_bars = VOLATILITY_AVG_WINDOW + VOLATILITY_ATR_PERIOD + 1
            rates = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_M1, 0, n_bars)
            if rates is None or len(rates) < n_bars:
                return False
            
            def calc_atr(start_idx: int, period: int) -> float:
                trs = []
                for i in range(start_idx, start_idx + period):
                    h, l, c = rates[i]['high'], rates[i]['low'], rates[i-1]['close'] if i > 0 else rates[i]['open']
                    tr = max(h - l, abs(h - c), abs(l - c))
                    trs.append(tr)
                return sum(trs) / len(trs) if trs else 0.0
            
            recent_atr = calc_atr(len(rates) - VOLATILITY_ATR_PERIOD, VOLATILITY_ATR_PERIOD)
            avg_atrs = []
            for i in range(VOLATILITY_ATR_PERIOD, len(rates) - VOLATILITY_ATR_PERIOD):
                avg_atrs.append(calc_atr(i, VOLATILITY_ATR_PERIOD))
            avg_atr = sum(avg_atrs) / len(avg_atrs) if avg_atrs else recent_atr
            
            if avg_atr <= 0:
                return False
            
            ratio = recent_atr / avg_atr
            return ratio >= self.volatility_atr_mult
        except Exception:
            return False

    def _is_blocked_time(self) -> bool:
        """指定時間帯（ブローカー時刻）ならTrue"""
        try:
            # Prefer monitor-cached server timestamp (GUI thread safe).
            tick_time = int(getattr(self, "_last_tick_time", 0) or 0)
            if tick_time <= 0:
                tick = mt5.symbol_info_tick(self.symbol)
                if not tick or not tick.time:
                    return False
                tick_time = int(tick.time)
            import datetime
            broker_time = datetime.datetime.utcfromtimestamp(tick_time)
            hour = broker_time.hour
            
            start = self.block_time_start_hour
            end = self.block_time_end_hour
            
            if start <= end:
                return start <= hour < end
            else:
                return hour >= start or hour < end
        except Exception:
            return False

    def _close_opposite_side(self, new_dir: int) -> int:
        """新しい上位TF方向 new_dir(+1/-1) に対し逆サイドを全部捨てる"""
        if new_dir == 0:
            return 0
        want_type = mt5.POSITION_TYPE_SELL if new_dir > 0 else mt5.POSITION_TYPE_BUY  # 逆サイド
        # [FIX] Isolation
        poss = self._get_my_positions()
        opp = [p for p in poss if p.type == want_type]
        # 任意のガード（大量クローズの安全弁）
        max_n = int(getattr(self, "dump_max_n", 9999))
        opp = opp[:max_n]
        return self._close_list(opp)  # 
        
    def _pivot_fire(self, side: str, origin: str, lots: float, n: int = 1) -> bool:
        ok, why = self._risk_allows_new()
        if not ok:
            self._set_status(f"Pivot skip ({origin}) risk guard: {why}")
            return False

        import MetaTrader5 as mt5
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            return False
        price = float(tick.ask if side == "buy" else tick.bid)
        if self._is_too_close_same_side(side, price, origin=origin):
            return False

        # ★v10.6: ナンピン防止ガード (ヘッジ系originは対象外)
        _HEDGE_ORIGINS = {"m15_flip_hedge", "pairnet", "nanpin_hedge"}
        if origin not in _HEDGE_ORIGINS and (
            self._is_nanpin_prevented(side)
            or self._is_majority_nanpin_filtered(side)
        ):
            self._set_status(f"Pivot skip ({origin}) nanpin/majority filter: {side}")
            return False

        info = mt5.symbol_info(self.symbol)
        vol  = self._norm_vol(max(getattr(info, "volume_min", 0.01) or 0.01,
                                float(getattr(self, "lot", 0.01))))
        ord_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL

        sent_any = False
        for k in range(n):
            ret = self._order_send_with_retry({
                "action":    mt5.TRADE_ACTION_DEAL,
                "symbol":    self.symbol,
                "volume":    vol,
                "type":      ord_type,
                "price":     round(price, self.digits),
                "deviation": DEVIATION,
                "magic":     self.magic,
                "comment":   f"{origin}-{side}{'' if n==1 else f'-{k+1}'}",
                "origin":    origin,
            })
            sent_any = bool(ret) or sent_any
        return sent_any
    def _get_rates(self, tf: str = "M1", n: int = 2):
        """
        最新n本のレートを取得して、[{time, open, high, low, close}] の配列で返す。
        timeはepoch秒（UTC）。取得失敗時は空配列を返す。
        """
        try:
            import MetaTrader5 as mt5
            tf = str(tf).upper()
            tf_map = {
                "M1":  getattr(mt5, "TIMEFRAME_M1", 1),
                "M5":  getattr(mt5, "TIMEFRAME_M5", 5),
                "M15": getattr(mt5, "TIMEFRAME_M15", 15),
                "M30": getattr(mt5, "TIMEFRAME_M30", 30),
                "H1":  getattr(mt5, "TIMEFRAME_H1", 60),
                "H4":  getattr(mt5, "TIMEFRAME_H4", 240),
                "D1":  getattr(mt5, "TIMEFRAME_D1", 1440),
            }
            tf_const = tf_map.get(tf)
            if tf_const is None:
                return []

            symbol = getattr(self, "symbol", None) or getattr(self, "SYMBOL", None)
            if not symbol:
                return []

            # 直近からn本（不足時もその分だけ返る）
            rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, int(max(1, n)))
            if rates is None or len(rates) == 0:
                return []

            out = []
            for r in rates:
                out.append({
                    "time":  int(r["time"]),
                    "open":  float(r["open"]),
                    "high":  float(r["high"]),
                    "low":   float(r["low"]),
                    "close": float(r["close"]),
                })
            return out
        except Exception:
            return []


    def _pivot_is_strict_match(self, d_exec: int, d_c1: int, d_c2: int) -> bool:
        """Pivot Strict一致判定（C1スキップ設定を考慮）"""
        if d_c2 == 0 or d_exec == 0:
            return False
        if getattr(self, "pivot_strict_skip_c1", PIVOT_STRICT_SKIP_C1):
            return d_c2 == d_exec
        if d_c1 == 0:
            return False
        return d_c2 == d_c1 == d_exec

    def _pivot_entry_check(self) -> None:
        """
        改良版 Pivot（動的CD付き）
        ────────────────────────────────
        """
        if self.trading_paused: return  # ★v10: 一時停止

        if not getattr(self, "pivot_enable", True):
            return

        import time
        now = time.time()
        if self._is_chop_blocked():
            remain = float(getattr(self, "_chop_until_ts", 0.0)) - now
            if remain > 0:
                try:
                    self._set_status(f"CHOP: entry paused ({remain:.0f}s)")
                except Exception:
                    pass
                return
        block_sec = float(getattr(self, "pivot_block_after_offset_sec", 0.0))
        last_block = float(getattr(self, "_last_offset_block_ts", 0.0))
        if block_sec > 0 and last_block > 0:
            # ポジションゼロなら即エントリー許可（過剰再発を避けつつ、空のときは自由）
            # [FIX] Isolation
            poss_now = self._get_cached_positions()
            if poss_now:
                remain = block_sec - (now - last_block)
                if remain > 0:
                    try:
                        self._set_status(f"Pivot blocked after offset ({remain:.0f}s)")
                    except Exception:
                        pass
                    return
        base_cd = float(getattr(self, "pivot_cooldown_sec", 60.0))
        cd = base_cd

        # ── Nanpin時はM1プロファイル相当の設定にフォールバック（TF/CD/Hold） ──
        m1_profile = None
        try:
            m1_profile = self.profiles.get("Scalp (M1)")
        except Exception:
            m1_profile = None

        nanpin_override = False
        try:
            nanpin_override = (self._nanpin_lock and getattr(self.profile, "exec_tf", None) in (mt5.TIMEFRAME_M5, mt5.TIMEFRAME_M15))
        except Exception:
            nanpin_override = False

        exec_tf_logic = self.profile.exec_tf
        c1_tf_logic = self.profile.c1_tf
        c2_tf_logic = self.profile.c2_tf
        ref1_tf_logic = self.profile.ref1_tf
        ref2_tf_logic = self.profile.ref2_tf
        hold_sec_logic = getattr(self.profile, "hold_sec", 55.0)

        if nanpin_override and m1_profile:
            exec_tf_logic = m1_profile.exec_tf
            c1_tf_logic = m1_profile.c1_tf
            c2_tf_logic = m1_profile.c2_tf
            ref1_tf_logic = m1_profile.ref1_tf
            ref2_tf_logic = m1_profile.ref2_tf
            hold_sec_logic = getattr(m1_profile, "hold_sec", hold_sec_logic)
            cd = float(getattr(m1_profile, "cd_sec", cd))

        # --- 現在の有効方向(+1/-1/0)を取得 ---
        # --- 現在の有効方向(+1/-1/0)を取得 (Relative) ---
        exec_s = self._get_tf_str(exec_tf_logic)
        c1_s   = self._get_tf_str(c1_tf_logic)
        c2_s   = self._get_tf_str(c2_tf_logic)
        ref1_s = self._get_tf_str(ref1_tf_logic) # Assist TF
        
        d1,  rh1,  rl1,  ev1,  bb1  = self._tf_dir(exec_s)   # Exec TF (Relative)
        d5,  rh5,  rl5,  _,    _    = self._tf_dir(c1_s)     # C1 TF (Relative)
        d60, rh60, rl60, _,    _    = self._tf_dir(c2_s)     # C2 TF (Relative)
        dH1x, *_ = self._tf_dir(ref1_s)  # Assist TF (Relative)

        # ── Nanpin時にM5/M15プロファイルをM1基準へフォールバック ──
        nanpin_override = exec_s in ("M5", "M15") and bool(getattr(self, "_nanpin_lock", False))
        m1_dir = bb_m1 = None
        if nanpin_override:
            m1_dir, _, _, _, bb_m1 = self._tf_dir("M1")
        
        # Relative variables for logic
        d_exec, d_c1, d_c2, d_ref1 = d1, d5, d60, dH1x
        rh_c1, rl_c1, rh_c2, rl_c2 = rh5, rl5, rh60, rl60

        # ピボット判定用の実効足をNanpin時だけM1に切替え
        d_exec_for_pivot = d_exec
        d_c1_for_pivot = d_c1
        d_c2_for_pivot = d_c2
        if nanpin_override and m1_dir is not None:
            d_exec_for_pivot = m1_dir
            d_c1_for_pivot = m1_dir
            d_c2_for_pivot = m1_dir

        # ★ 追加：dump flip は H4 をデフォルト（必要なら D1 も選択可）
        dump_tf = str(getattr(self, "dump_flip_tf", "H4")).upper()
        dDump = 0
        if dump_tf == "H4":
            dDump, *_ = self._tf_dir("H4")
        elif dump_tf == "D1":
            dDump, *_ = self._tf_dir("D1")


        # 方向取得の直後に追加
        self._clear_pivot_ladder_anchor_if_needed()

        # --- 進行中バーの「両抜け」判定で上位TFを見る補助 ---
        def _cur_bar_high_low(tf_const):
            try:
                r = mt5.copy_rates_from_pos(self.symbol, tf_const, 0, 1)
                if r is not None and len(r) > 0:
                    return float(r[0]["high"]), float(r[0]["low"])
            except Exception:
                return None, None
            return None, None

        hi_c2, lo_c2 = _cur_bar_high_low(c2_tf_logic)
        hi_c1, lo_c1 = _cur_bar_high_low(c1_tf_logic)

        is_c2_both = bool(rh_c2 and rl_c2 and hi_c2 is not None and lo_c2 is not None
                           and hi_c2 >= rh_c2 and lo_c2 <= rl_c2)
        is_c1_both = bool(rh_c1 and rl_c1 and hi_c1 is not None and lo_c1 is not None
                           and hi_c1 >= rh_c1 and lo_c1 <= rl_c1)
        is_m15_both = is_c2_both # Legacy alias
        is_m5_both = is_c1_both # Legacy alias
        # --- UI更新（任意） ---
        try:
            self._refresh_tf_dir_ui()
        except Exception:
            pass


        # --- Rangeフィルタ ---
        # 削除: M5/M1 Syncロジック内で個別に判断するため、ここでは一括リターンしない
        pass


        # --- 送信ヘルパ ---
        def _fire(side: str, origin: str, lots: float, n: int = 1) -> None:
            # ── Candle Color Check (Exec Confirmed Bar) ──
            # Enforce strict color check for ALL pivot entries
            if not self._verify_exec_candle_color(side, exec_tf=exec_tf_logic):
                return

            # ── Smart Close エントリーフィルター ─────────────────
            sc_block_reason = self._sc_entry_block_reason(side)
            if sc_block_reason:
                self._set_status(sc_block_reason)
                return

            # ── entry budget（決済回転率ベース） ────────────────
            ok_budget, budget = self._entry_budget_check(side)
            if not ok_budget:
                if (time.time() - float(getattr(self, "_last_entry_budget_log_ts", 0.0))) >= 3.0:
                    self._last_entry_budget_log_ts = time.time()
                    try:
                        use_budget_flag = bool(getattr(self, "ENTRY_BUDGET_ENABLE", ENTRY_BUDGET_ENABLE))
                        reason_txt = budget.get("reason", "budget")
                        tag = "Entry limited" if use_budget_flag else "Entry blocked (guard)"
                        self._set_status(
                            f"{tag} ({origin}) {budget.get('entries_win',0)}/{budget.get('limit',0)} "
                            f"(cl={budget.get('closes_win',0)}) reason={reason_txt} "
                            f"Dirs[{exec_s}:{d1}/{c1_s}:{d5}/{c2_s}:{d60}]"
                        )
                    except Exception:
                        pass
                return

            # ── ML gate (First Position Only) ──
            # 1ポジ目（pos_n == 0）の時だけ厳密にAI判定を行う。
            # 2ポジ目以降（救済/ナンピン）はAIをスルーしてメカニカルに動く。
            
            # 実際のポジション数取得（budgetチェック時のキャッシュ等は使わず再取得推奨だが、ここでは軽量化のため既存情報があれば使う）
            # ただし _entry_budget_check は数を返さないので、ここできちんと確認
            current_pos_n = 0
            try:
                # [FIX] Isolation
                poss = self._get_my_positions()
                current_pos_n = len(poss)
            except Exception:
                pass

            ai_apply = bool(self.ai_active and self.ai_enable and not bool(budget.get("protective", False)))
            
            # ★ 変更: ポジションがある場合は AI Bypass
            if current_pos_n > 0:
                ai_apply = False
            else:
                 # 1ポジ目はAI適用（ただしCloseが少ない時だけ、という既存OPがあれば従う）
                 if ai_apply:
                    try:
                        ai_apply = bool(int(budget.get("closes_win", 0)) < int(AI_APPLY_WHEN_CLOSE_LT))
                    except Exception:
                        ai_apply = True

            feat = None
            prob = None
            passed = True
            if ai_apply:
                feat = self._ai_feature_snapshot(
                    side=side,
                    d1=int(d1),
                    d5=int(d5),
                    d15=int(d60),
                    dH1=int(dH1x),
                    r1=bool(d1==0),
                    r5=bool(d5==0),
                    r15=bool(d60==0),
                    is_m5_both=bool(is_c1_both),
                    is_m15_both=bool(is_c2_both),
                    m15_rh=float(rh60) if rh60 is not None else None,
                    m15_rl=float(rl60) if rl60 is not None else None,
                    origin=str(origin),
                )
                prob = self._ai_predict_prob(feat)
                if prob is None:
                    passed = bool(AI_FAIL_OPEN)
                else:
                    passed = bool(prob >= float(self.ai_threshold))
                if not passed:
                    try:
                        ptxt = "NA" if prob is None else f"{prob:.2f}"
                        self._set_status(f"AI blocked ({origin}) p={ptxt}<{self.ai_threshold:.2f}")
                        if not self.headless and getattr(self, "_mon_vars", None):
                            self._safe_set(self._mon_vars["ai_prob"], f"p={ptxt} (apply=1)")
                    except Exception:
                        pass
                    # 学習用ログは残す
                    self._ai_log_row(
                        feat,
                        prob,
                        False,
                        {
                            "origin": origin,
                            "protective": bool(budget.get("protective", False)),
                            "closes_win": int(budget.get("closes_win", 0) or 0),
                            "entries_win": int(budget.get("entries_win", 0) or 0),
                            "entry_limit": int(budget.get("limit", 0) or 0),
                            "ai_apply": True,
                            "price": (lambda t: (t.ask if side.lower() == "buy" else t.bid) if t else 0.0)(mt5.symbol_info_tick(self.symbol)),
                        },
                    )
                    return

            # ログ（AIを使っていない場合でも、特徴量が取れるなら残す）
            if self.ai_log_enable:
                if feat is None:
                    try:
                        feat = self._ai_feature_snapshot(
                            side=side,
                            d1=int(d1),
                            d5=int(d5),
                            d15=int(d60),
                            dH1=int(dH1x),
                            r1=bool(r1),
                            r5=bool(r5),
                            r15=bool(r15),
                            is_m5_both=bool(is_m5_both),
                            is_m15_both=bool(is_m15_both),
                            m15_rh=float(rh60) if rh60 is not None else None,
                            m15_rl=float(rl60) if rl60 is not None else None,
                            origin=str(origin),
                        )
                    except Exception:
                        feat = None
                if isinstance(feat, dict):
                    self._ai_log_row(
                        feat,
                        prob,
                        True,
                        {
                            "origin": origin,
                            "protective": bool(budget.get("protective", False)),
                            "closes_win": int(budget.get("closes_win", 0) or 0),
                            "entries_win": int(budget.get("entries_win", 0) or 0),
                            "entry_limit": int(budget.get("limit", 0) or 0),
                            "ai_apply": bool(ai_apply),
                            "price": (lambda t: (t.ask if side.lower() == "buy" else t.bid) if t else 0.0)(mt5.symbol_info_tick(self.symbol)),
                        }

                    )
            if not self.headless and getattr(self, "_mon_vars", None):
                try:
                    if ai_apply:
                        if prob is None:
                            self._safe_set(self._mon_vars["ai_prob"], "p=NA (apply=1)")
                        else:
                            self._safe_set(self._mon_vars["ai_prob"], f"p={prob:.2f} (apply=1)")
                    else:
                        self._safe_set(self._mon_vars["ai_prob"], "p=— (apply=0)")
                except Exception:
                    pass

            ok, why = self._risk_allows_new()
            if not ok:
                self._set_status(f"Pivot skip ({origin}) risk guard: {why}")
                return

            tick = mt5.symbol_info_tick(self.symbol)
            if not tick:
                return
            price = float(tick.ask if side == "buy" else tick.bid)
            if self._is_too_close_same_side(side, price, origin=origin):
                return

            info = mt5.symbol_info(self.symbol)
            vol  = self._norm_vol(max(getattr(info, "volume_min", 0.01) or 0.01,
                                      float(getattr(self, "lot", 0.01))))
            # ▼▼▼ Pyramid Entry Filter (Re-implemented) ▼▼▼
            # [FIX] Isolation
            poss = self._get_my_positions()
            is_pyramid_attempt = False
            avg_price_current = 0.0

            if len(poss) > 0:
                # [FIX] Pyramid/Nanpin Cooldown Check (Unified Mode Support)
                # Ensure we don't stack positions in the same candle (or too rapidly).
                req_cd_sec = float(getattr(self, "pivot_cooldown_sec", 55.0))
                
                # Determine "Boss" timeframe from first position comment
                try:
                    c_str = str(poss[0].comment).lower()
                    if "pivot_strict" in c_str:     # M15
                        req_cd_sec = 895.0
                    elif "pivot_m5both" in c_str:   # M5
                        req_cd_sec = 295.0
                    elif "pivot_h1assist" in c_str: # H1
                        req_cd_sec = 295.0
                except: pass
                
                last_fire = float(getattr(self, "_last_pivot_fire_ts", 0.0))
                now_ts = time.time()
                
                if (now_ts - last_fire) < req_cd_sec:
                    # CD Blocked
                    return

                # Filter positions by side to calculate relevant average
                target_type = mt5.POSITION_TYPE_BUY if side == "buy" else mt5.POSITION_TYPE_SELL
                side_poss = [p for p in poss if p.type == target_type]
                
                if len(side_poss) > 0:
                    # Calculate current weighted average price for this side
                    total_vol = sum(p.volume for p in side_poss)
                    total_cost = sum(p.volume * p.price_open for p in side_poss)
                    avg_price_current = total_cost / total_vol if total_vol > 0 else 0.0
                    
                    # Determine if this entry is "Pyramiding" (adding to winners) or "Averaging" (adding to losers)
                    is_buy = (side == "buy")
                    
                    if is_buy:
                        # Expecting price > avg for pyramid (adding on way up)
                        if price > avg_price_current:
                            is_pyramid_attempt = True
                    else:
                        # Expecting price < avg for pyramid (adding on way down)
                        if price < avg_price_current:
                            is_pyramid_attempt = True
                else:
                    # No positions on this side -> Not pyramiding (First entry for this side)
                    pass
                
                if is_pyramid_attempt:
                    # Check Projected Profit at Exit (1m RL or RH)
                    _d1, _rh1, _rl1, _ev1, _ = self._tf_dir("M1")
                    
                    exit_price = 0.0
                    if is_buy:
                        exit_price = float(_rl1) if _rl1 is not None else (price - 100 * (info.point or 0.001)) # Fallback
                    else:
                        exit_price = float(_rh1) if _rh1 is not None else (price + 100 * (info.point or 0.001)) # Fallback

                    # Calculate new average if we add this position
                    new_vol_total = total_vol + vol;
                    new_avg_price = (total_cost + (vol * price)) / new_vol_total
                    
                    # Projected PnL at Exit Price
                    projected_pnl = 0.0
                    if is_buy:
                         projected_pnl = (exit_price - new_avg_price) * new_vol_total
                    else:
                         projected_pnl = (new_avg_price - exit_price) * new_vol_total
                    
                    if projected_pnl < 0:
                        self._log(f"[PYRAMID WARN] {side} @{price:.3f}: Projected PnL {projected_pnl:.2f} < 0 at M1 Exit {exit_price:.3f}. Avg {avg_price_current:.3f}->{new_avg_price:.3f}. Allowing entry.", level=1)
                        # We do NOT block entry anymore, relying on Trailing Exit to manage risk.
                    else:
                        self._log(f"[PYRAMID ALLOW] {side} @{price:.3f}: Est PnL {projected_pnl:.2f} > 0 at M1 Exit {exit_price:.3f}", level=1)
                
                else:
                    # Nanpin (Averaging) - Allow standard logic
                    pass

            # Body-break guards for Pyramid entries (exec TF + upper TF)
            if is_pyramid_attempt:
                pyramid_body_ok = True
                pyramid_upper_ok = True
                exec_tf_str = "M1"
                try:
                    exec_tf_str = exec_s
                except Exception:
                    pass
                try:
                    _, _, _, _, bb_exec = self._tf_dir(exec_tf_str)
                    if side == "buy" and bb_exec <= 0:
                        pyramid_body_ok = False
                        self._log(f"[PYRAMID GUARD] Blocked: waiting for {exec_tf_str} body break BUY (bb={bb_exec})", tag="PIVOT", level=1)
                    elif side == "sell" and bb_exec >= 0:
                        pyramid_body_ok = False
                        self._log(f"[PYRAMID GUARD] Blocked: waiting for {exec_tf_str} body break SELL (bb={bb_exec})", tag="PIVOT", level=1)
                except Exception as e:
                    self._log(f"[PYRAMID GUARD] Exec TF body break check error: {e}", tag="PIVOT", level=2)

                try:
                    upper_map = {"M1": "M15", "M5": "H1", "M15": "H4"}
                    upper_tf = upper_map.get(exec_tf_str)
                    if upper_tf:
                        _, _, _, _, bb_upper = self._tf_dir(upper_tf)
                        if side == "buy" and bb_upper <= 0:
                            pyramid_upper_ok = False
                            self._log(f"[PYRAMID GUARD] Blocked: waiting for {upper_tf} body break BUY (bb={bb_upper})", tag="PIVOT", level=1)
                        elif side == "sell" and bb_upper >= 0:
                            pyramid_upper_ok = False
                            self._log(f"[PYRAMID GUARD] Blocked: waiting for {upper_tf} body break SELL (bb={bb_upper})", tag="PIVOT", level=1)
                except Exception as e:
                    self._log(f"[PYRAMID GUARD] Upper TF body break check error: {e}", tag="PIVOT", level=2)

                if not (pyramid_body_ok and pyramid_upper_ok):
                    return

            for k in range(n):
                self._send_entry_with_limit(
                    side=side,
                    vol=vol,
                    origin=origin,
                    price_hint=price,
                    prefer_limit=False,
                    comment=f"{origin}-{side}{'' if n==1 else f'-{k+1}'}",
                )
                self._note_entry_event(count=1, side=side)

        # --- 直前方向の取得 ---
        prev_h1   = getattr(self, "_last_h1_dir",  None)
        prev_M15  = getattr(self, "_last_M15_dir", None)
        prev_M5   = getattr(self, "_last_M5_fast", None)
        # M15フリップ検知（CD短縮用タイムスタンプ）
        if prev_M15 is not None and d60 in (-1, +1) and d60 != prev_M15:
            self._last_pivot_flip_ts = now
            self._note_dir_flip("M15", now)
        # === ★ 上位TFフリップで逆サイドを捨てる ===
        # === ★ 上位TFフリップで逆サイドを捨てる（即時反応のためゲート外へ移動） ===
        if getattr(self, "dump_flip_enable", False):
            dir_up = int(dDump)
            prev_up = getattr(self, "_last_dump_dir", None)
            
            # 初回は現状維持
            if prev_up is None:
                self._last_dump_dir = int(dir_up)
            
            # 方向が明確(-1/1)かつ、前回と異なり、かつ前回も明確(-1/1)だった場合などに限定もできるが、
            # ここではシンプルに「方向変化」でトリガー
            elif dir_up in (-1, +1) and (dir_up != prev_up) and (prev_up != 0):
                last_ts = float(getattr(self, "_last_dump_flip_ts", 0.0))
                cd_dump = float(getattr(self, "dump_flip_cooldown_sec", 0.0))
                
                # Cooldownチェック
                if (now - last_ts) >= cd_dump:
                    # 逆サイド決済
                    n_closed = self._close_opposite_side(dir_up)
                    if n_closed > 0:
                        self._log(f"[DUMP] {dump_tf} Flip ({prev_up} -> {dir_up}) -> Closed {n_closed} opposite pos", level=1)
                    
                    # Flip直後のプローブ
                    if bool(getattr(self, "dump_fire_probe", False)):
                         side = "buy" if dir_up > 0 else "sell"
                         _fire(side, origin=f"dump-{dump_tf.lower()}-flip", lots=float(getattr(self, "lot", 0.01)), n=1)

                    self._last_dump_flip_ts = now
            
            # 状態更新
            if dir_up != 0:
                self._last_dump_dir = int(dir_up)

        # ── Entry Timing Gate (All Positions) ──
        # エントリー（新規・追撃）はExec TF確定足のみ
        # Nanpin時はexec_tf_logic（M1）に合わせる
        if not self._bar_gate("pivot_entry", exec_tf_logic):
             return


        # # === ① H1の方向が変わったら 1本 ===
        # if d60 in (-1, 0, +1):
        #     if (prev_h1 is not None) and (d60 != 0) and (d60 != prev_h1):
        #         if (now - float(getattr(self, "_last_pivot1h_ts", 0.0))) >= cd:
        #             side = "buy" if d60 > 0 else "sell"
        #             _fire(side, origin="pivot1h", lots=float(getattr(self, "lot", 0.01)), n=1)
        #             self._last_pivot1h_ts = now
        # === ★ M15フリップ時に部分ヘッジを入れて“挟む”オプション ===
        if getattr(self, "pivot_m15_flip_hedge_enable", False):
            prev_M15_real = getattr(self, "_last_M15_real_dir", None)  # 新規: 本物のM15方向
            if d60 in (-1, 0, +1):
                if (prev_M15_real is not None) and (d60 != 0) and (d60 != prev_M15_real):
                    # ここが M15 flip イベント
                    # 例）+1→-1 に変わった瞬間など
                    try:
                        # 現在のポジション一覧
                        # [FIX] Isolation
                        poss = self._get_my_positions()
                        if poss:
                            if d60 > 0:
                                # 新方向: BUY → 逆サイドは SELL
                                opp_type  = mt5.POSITION_TYPE_SELL
                                side_str  = "buy"
                                order_type = mt5.ORDER_TYPE_BUY
                            else:
                                # 新方向: SELL → 逆サイドは BUY
                                opp_type  = mt5.POSITION_TYPE_BUY
                                side_str  = "sell"
                                order_type = mt5.ORDER_TYPE_SELL

                            cnt_opp = sum(1 for p in poss if int(p.type) == opp_type)
                            same_type = mt5.POSITION_TYPE_BUY if side_str == "buy" else mt5.POSITION_TYPE_SELL
                            cnt_same = sum(1 for p in poss if int(p.type) == same_type)

                            # ★ 偏り = 逆方向本数 - 同方向本数
                            # 例) buy20, sell5 で新方向が sell のとき:
                            #     opp_type=BUY → cnt_opp=20, same_type=SELL → cnt_same=5
                            #     imbalance = max(20 - 5, 0) = 15
                            imbalance = max(cnt_opp - cnt_same, 0)

                            if imbalance > 0:
                                k = float(getattr(self, "pivot_m15_flip_hedge_ratio", 0.5))  # 例: 0.5 (=偏りの半分だけ挟む)
                                max_batch = int(getattr(self, "pivot_m15_flip_hedge_max_batch", 5))

                                # 偏りに対して k 倍 → (cnt_opp - cnt_same) * k
                                n_raw = int(imbalance * max(0.0, k))

                                # ★ pivot_stream が同方向に1本だけ入ってくる前提で、
                                #    そのぶん 1 本“席を空けておく”
                                reserve_for_stream = 1
                                n_hedge = max(0, min(max_batch, n_raw - reserve_for_stream))


                                if n_hedge > 0:
                                    base_lot = float(getattr(self, "lot", 0.01))
                                    lots = self._norm_vol(base_lot)


                                    # リスクガード一回チェック（ダメなら全部キャンセル）
                                    ok, why = self._risk_allows_new()
                                    if not ok:
                                        self._set_status(f"M15 flip hedge skip: {why}")
                                    else:
                                        for _ in range(n_hedge):
                                            tick = mt5.symbol_info_tick(self.symbol)
                                            if not tick:
                                                break
                                            price = float(tick.ask if side_str == "buy" else tick.bid)


                                            self._order_send_with_retry({
                                                "action":   mt5.TRADE_ACTION_DEAL,
                                                "symbol":   self.symbol,
                                                "volume":   lots,
                                                "type":     order_type,
                                                "price":    round(price, self.digits),
                                                "deviation": DEVIATION,
                                                "magic":    self.magic,
                                                "comment":  "m15-flip-hedge",
                                                "origin":   "m15_flip_hedge",
                                            })

                                        self._log(
                                            f"[M15-FLIP-HEDGE] dir={d60} opp_cnt={cnt_opp} hedge={n_hedge}",
                                            level=1
                                        )
                    except Exception as e:
                        self._log(f"[M15-FLIP-HEDGE] error: {e}", level=1)

            # 本物のM15方向を更新
            if d60 in (-1, 0, +1):
                self._last_M15_real_dir = int(d60)
                
        # UI補助: 両抜け補助の状態を表示
        if not self.headless and hasattr(self, "_mon_vars"):
            try:
                self._safe_set(
                    self._mon_vars["pivot_flags"],
                    f"M5both:{'Y' if is_m5_both else 'N'}  M15both:{'Y' if is_m15_both else 'N'}  H1:{dH1x:+d if isinstance(dH1x,int) else dH1x}",
                )
            except Exception:
                pass

        # --- エントリー条件 ---
        # ★変更: M15-M1一致のみを見る (User Request: Move focus to M15)
        
        # 状態取得
        st15 = self._pivot_state.get("M15", {})
        m15_mode = st15.get("mode", "")
        
        # --- Provisional H1 Follow Logic (User Request) ---
        # M15が確定前でも、現在値がレンジをブレイクしており、かつH1と同方向なら採用する
        # ★一時性: このOverrideは「進行中のM15バー限定」。バーが変わったらリセット。
        if getattr(self, "pivot_h1_on_m15_neutral", True) and isinstance(dH1x, int) and dH1x != 0:
            try:
                # 現在のM15バー開始時刻を取得
                cur_m15_bar_ts = self._last_closed_bar_time(mt5.TIMEFRAME_M15) or 0
                prev_override_bar_ts = getattr(self, "_h1_override_m15_bar_ts", 0)
                
                # M15バーが変わったらOverride状態をリセット
                if cur_m15_bar_ts != prev_override_bar_ts:
                    self._h1_override_active = False
                
                rh15 = float(st15.get("rh")) if st15.get("rh") is not None else None
                rl15 = float(st15.get("rl")) if st15.get("rl") is not None else None
                cur_p = self.mid
                
                # Check Up Break
                if dH1x == 1 and rh15 is not None and cur_p > rh15:
                    d60 = 1 # Override M15 to Up
                    self._h1_override_active = True
                    self._h1_override_m15_bar_ts = cur_m15_bar_ts
                # Check Down Break
                elif dH1x == -1 and rl15 is not None and cur_p < rl15:
                    d60 = -1 # Override M15 to Down
                    self._h1_override_active = True
                    self._h1_override_m15_bar_ts = cur_m15_bar_ts
            except Exception:
                pass
        # --------------------------------------------------
        
        # Sync Logic (M15 drives M1)
        # Old Sync logic removed as M1 now has native Range-Bar logic in _tf_dir

        # Sync Logic (Dynamic)
        # Old Sync logic removed as Exec TF now has native Range-Bar logic in _tf_dir

        st1 = self._pivot_state.get(exec_s, {})
        st5 = self._pivot_state.get(c1_s, {})
        st15 = self._pivot_state.get(c2_s, {})

        # Update Logic State for GUI
        self._pivot_logic_state = {
            "M1": d1,   # Label kept as M1 for compatibility, but holds Exec data
            "M1_RH": rh1,
            "M1_RL": rl1,
            "M1_Mode": st1.get("mode", ""),
            "M1_Ev": st1.get("ev", ""),
            "M1_Ovrd": st1.get("dir_override") is not None,

            "M5": d5,
            "M5_RH": rh5,
            "M5_RL": rl5,
            "M5_Mode": st5.get("mode", ""),
            "M5_Ev": st5.get("ev", ""),
            "M5_Ovrd": st5.get("dir_override") is not None,

            "M15": d60,
            "M15_RH": rh60,
            "M15_RL": rl60,
            "M15_Mode": st15.get("mode", ""),
            "M15_Ev": st15.get("ev", ""),
            "M15_Ovrd": st15.get("dir_override") is not None,
            
            "H1_Ref_Val": dH1x,
            "H1_Active": (getattr(self, "pivot_h1_on_m15_neutral", True) and isinstance(dH1x, int) and dH1x != 0),
            "H1_Ovrd_M15": (d60 != int(st15.get("dir", 0))) # Track the H1-M15 breakout override
        }
        

        # ── Unified Pivot Logic (Integrated Patterns) ──
        
        # 1. Determine Trigger Conditions
        # A) Strict Match (Self=C1=C2) or Skip C1 Mode (Self=C2 only)
        is_strict = self._pivot_is_strict_match(d_exec_for_pivot, d_c1_for_pivot, d_c2_for_pivot)
        
        # B) C1 Both Break (e.g. M5 Both for M1 bot)
        is_c1_both_cond = (is_c1_both and d_c2_for_pivot in (-1, +1) and d_exec_for_pivot == d_c2_for_pivot)
        
        # C) C2 Assist / Both Break (e.g. M15 Both for M1 bot, triggered by H1 Assist)
        cond_ref1assist = False
        if (d_c2_for_pivot == 0 or is_c2_both) and d_ref1 in (-1, +1):
            cond_ref1assist = (d_c1_for_pivot == d_ref1) and (d_exec_for_pivot == d_ref1)

        triggered = False
        target_side = None
        origin_mark = "pivot_std"
        
        if is_strict:
            target_side = "buy" if d_c2_for_pivot > 0 else "sell"
            triggered = True
            origin_mark = "pivot_strict"
        elif is_c1_both_cond:
            target_side = "buy" if d_c2_for_pivot > 0 else "sell"
            triggered = True
            origin_mark = "pivot_m5both"
        elif cond_ref1assist:
            target_side = "buy" if d_ref1 > 0 else "sell"
            triggered = True
            origin_mark = "pivot_h1assist"
            
        if not triggered or not target_side:
            # 1M Pair-Net への移行などはメソッド最後で行われるため、ここでは単にPivotを抜ける
            pass
        else:
            # 2. Position Context Check (1st vs Add)
            current_pos_n = 0
            poss = []
            try:
                # [FIX] Isolation
                poss = self._get_my_positions()
                current_pos_n = len(poss)
            except Exception:
                pass

            is_entry_allowed = False
            
            if current_pos_n == 0:
                # === Case 1: 1st Entry (Strict Rules) ===
                # Already passed _bar_gate(self.profile.exec_tf) at the start of method
                guard_ok = True

                if guard_ok:
                    # Check Range Block (Dynamic Exec TF)
                    exec_tf_str = exec_s
                    exec_st = self._pivot_state.get(exec_tf_str, {})
                    
                    if exec_st.get("mode") == "range-inherit":
                        if (time.time() - float(getattr(self, "_last_range_block_log_ts", 0.0))) >= 5.0:
                            self._last_range_block_log_ts = time.time()
                            self._log(f"[GUARD] 1st Entry Blocked: {exec_tf_str} is Range", tag="PIVOT", level=1)
                    else:
                        # Check PA Match (Exec Candle Color)
                        rates_exec = mt5.copy_rates_from_pos(self.symbol, exec_tf_logic, 1, 1)
                        pa_match = False
                        if rates_exec is not None and len(rates_exec) > 0:
                            c = rates_exec[0]["close"]
                            o = rates_exec[0]["open"]
                            if target_side == "buy": pa_match = (c > o)
                            else: pa_match = (c < o)
                        
                        if not pa_match:
                            if not self.headless: 
                                lbl = "Bearish" if target_side == "sell" else "Bullish"
                                self._log(f"[PIVOT SKIP] {target_side.capitalize()} signal but {exec_tf_str} bar was not {lbl}", tag="PIVOT")
                        
                        if pa_match and self.pivot_first_entry_relax_enable:
                            relax_ok = True
                            # ★v10.7: Relax時もExec足の実体抜けは必須（緩和しすぎ防止）
                            # nanpin_override時はM1の実体抜けを基準にする
                            bb_check_tf = exec_s
                            if nanpin_override and bb_m1 is not None:
                                bb_check_tf = "M1"
                            try:
                                _, _, _, _, bb_exec_relax = self._tf_dir(bb_check_tf)
                                if target_side == "buy" and bb_exec_relax <= 0:
                                    relax_ok = False
                                    self._log(f"[GUARD] 1st Relax Blocked: {bb_check_tf} body break not BUY (bb={bb_exec_relax})", tag="PIVOT", level=1)
                                elif target_side == "sell" and bb_exec_relax >= 0:
                                    relax_ok = False
                                    self._log(f"[GUARD] 1st Relax Blocked: {bb_check_tf} body break not SELL (bb={bb_exec_relax})", tag="PIVOT", level=1)
                            except Exception as e:
                                relax_ok = False
                                self._log(f"[GUARD] 1st Relax exec body break check error ({bb_check_tf}): {e}", tag="PIVOT", level=2)
                            # nanpin_override時はM1基準のalign_mapを使う
                            relax_align_key = "M1" if nanpin_override else exec_s
                            align_map = {
                                "M1": ("M15", "H4"),
                                "M5": ("H1", "D1"),
                                "M15": ("H4", "W1"),
                            }
                            if relax_ok and relax_align_key in align_map:
                                try:
                                    tf_a, tf_b = align_map[relax_align_key]
                                    d_a_chk, _, _, _, _ = self._tf_dir(tf_a)
                                    d_b_chk, _, _, _, _ = self._tf_dir(tf_b)
                                    if target_side == "buy":
                                        if d_a_chk <= 0 or d_b_chk <= 0:
                                            relax_ok = False
                                    else:
                                        if d_a_chk >= 0 or d_b_chk >= 0:
                                            relax_ok = False
                                    if not relax_ok:
                                        self._log(
                                            f"[GUARD] 1st Relax Blocked: need {tf_a}+{tf_b} align ({target_side.upper()}) "
                                            f"({tf_a}={d_a_chk}, {tf_b}={d_b_chk})",
                                            tag="PIVOT",
                                            level=1,
                                        )
                                except Exception as e:
                                    relax_ok = False
                                    self._log(f"[GUARD] 1st Relax align check error ({exec_s}): {e}", tag="PIVOT", level=2)
                            if relax_ok:
                                is_entry_allowed = True
                                if origin_mark == "pivot_std":
                                    origin_mark = "pivot_1st_relax"
                                self._log(f"[PIVOT] 1st Entry Relax ON ({origin_mark})", tag="PIVOT")
                        elif pa_match:
                            # ★v10.1: Exec TF実体抜けチェック（1ポジ目のみ）
                            # [FIX] user request: Unifiedモード時はシグナル源に合わせて実体抜けを見る足を変える
                            body_break_ok = True
                            if PIVOT_FIRST_REQUIRE_BODY_BREAK:
                                try:
                                    # [Simplified] Always check body break on the execution timeframe
                                    check_tf = exec_s
                                    _, _, _, _, bb_val = self._tf_dir(check_tf)
                                    # bb_val > 0 = BUY方向の実体抜け, bb_val < 0 = SELL方向
                                    if target_side == "buy" and bb_val <= 0:
                                        body_break_ok = False
                                        self._log(f"[GUARD] 1st Entry Blocked: waiting for {check_tf} body break BUY (bb={bb_val})", tag="PIVOT", level=1)
                                    elif target_side == "sell" and bb_val >= 0:
                                        body_break_ok = False
                                        self._log(f"[GUARD] 1st Entry Blocked: waiting for {check_tf} body break SELL (bb={bb_val})", tag="PIVOT", level=1)
                                except Exception as e:
                                    self._log(f"[GUARD] Body break check error: {e}", tag="PIVOT", level=2)
                            
                            # ★v10.1: Ref2 TFトレンド一致チェック（1ポジ目のみ）
                            h4_trend_ok = True
                            if body_break_ok and PIVOT_FIRST_REQUIRE_H4_TREND:
                                try:
                                    tf_ref2 = self._get_tf_str(ref2_tf_logic)
                                    dH4, _, _, _, _ = self._tf_dir(tf_ref2)
                                    if target_side == "buy" and dH4 < 0:
                                        h4_trend_ok = False
                                        self._log(f"[GUARD] 1st Entry Blocked: {tf_ref2} trend DOWN vs BUY (dir={dH4})", tag="PIVOT", level=1)
                                    elif target_side == "sell" and dH4 > 0:
                                        h4_trend_ok = False
                                        self._log(f"[GUARD] 1st Entry Blocked: {tf_ref2} trend UP vs SELL (dir={dH4})", tag="PIVOT", level=1)
                                except Exception as e:
                                    self._log(f"[GUARD] Ref2 trend check error: {e}", tag="PIVOT", level=2)
                            
                            # ★v10.6: 環境認識足(Ref2)の実体抜けチェック（1ポジ目のみ）
                            ref2_body_ok = True
                            if body_break_ok and h4_trend_ok and self.pivot_first_require_ref2_body_break:
                                try:
                                    tf_ref2 = self._get_tf_str(ref2_tf_logic)
                                    _, _, _, _, bb_ref2 = self._tf_dir(tf_ref2)
                                    if target_side == "buy" and bb_ref2 <= 0:
                                        ref2_body_ok = False
                                        self._log(f"[GUARD] 1st Entry Blocked: {tf_ref2} body break not BUY (bb={bb_ref2})", tag="PIVOT", level=1)
                                    elif target_side == "sell" and bb_ref2 >= 0:
                                        ref2_body_ok = False
                                        self._log(f"[GUARD] 1st Entry Blocked: {tf_ref2} body break not SELL (bb={bb_ref2})", tag="PIVOT", level=1)
                                except Exception as e:
                                    self._log(f"[GUARD] Ref2 body break check error: {e}", tag="PIVOT", level=2)

                            # ★v10.6: Zigzagエントリー許可チェック（1ポジ目のみ）
                            zz_ok = True
                            if body_break_ok and h4_trend_ok and ref2_body_ok and self.pivot_zigzag_entry_enable:
                                if not self._zz_entry_flag:
                                    zz_ok = False
                                    sh_d = f"{self._zz_swing_high:.5f}" if self._zz_swing_high else "—"
                                    sl_d = f"{self._zz_swing_low:.5f}" if self._zz_swing_low else "—"
                                    self._log(f"[GUARD] 1st Entry Blocked: ZZ flag OFF (SH={sh_d} SL={sl_d})", tag="PIVOT", level=1)

                            # ★v10.1: Context1 TF実体抜けチェック（オプション）
                            m5_bb_ok = True
                            if body_break_ok and h4_trend_ok and ref2_body_ok and zz_ok and PIVOT_FIRST_REQUIRE_M5_BODY_BREAK:
                                try:
                                    tf_c1 = self._get_tf_str(c1_tf_logic)
                                    bb5, _, _, _, _ = self._tf_dir(tf_c1)
                                    if target_side == "buy" and bb5 <= 0:
                                        m5_bb_ok = False
                                        self._log(f"[GUARD] 1st Entry Blocked: waiting for {tf_c1} body break BUY (bb={bb5})", tag="PIVOT", level=1)
                                    elif target_side == "sell" and bb5 >= 0:
                                        m5_bb_ok = False
                                        self._log(f"[GUARD] 1st Entry Blocked: waiting for {tf_c1} body break SELL (bb={bb5})", tag="PIVOT", level=1)
                                except Exception as e:
                                    self._log(f"[GUARD] M5 body break check error: {e}", tag="PIVOT", level=2)

                            # ★追加: 上位足(監視足)の実体抜けチェック（1ポジ目のみ、v10.6: トグルで無効化可能）
                            upper_body_ok = True
                            if body_break_ok and h4_trend_ok and ref2_body_ok and zz_ok and m5_bb_ok and self.pivot_first_require_upper_body_break:
                                try:
                                    upper_map = {"M1": "M15", "M5": "H1", "M15": "H4"}
                                    upper_tf = upper_map.get(exec_s)
                                    if upper_tf:
                                        _, _, _, _, bb_val_up = self._tf_dir(upper_tf)
                                        if target_side == "buy" and bb_val_up <= 0:
                                            upper_body_ok = False
                                            self._log(f"[GUARD] 1st Entry Blocked: waiting for {upper_tf} body break BUY (bb={bb_val_up})", tag="PIVOT", level=1)
                                        elif target_side == "sell" and bb_val_up >= 0:
                                            upper_body_ok = False
                                            self._log(f"[GUARD] 1st Entry Blocked: waiting for {upper_tf} body break SELL (bb={bb_val_up})", tag="PIVOT", level=1)
                                except Exception as e:
                                    self._log(f"[GUARD] Upper TF body break check error: {e}", tag="PIVOT", level=2)

                            if body_break_ok and h4_trend_ok and ref2_body_ok and zz_ok and m5_bb_ok and upper_body_ok:
                                is_entry_allowed = True
                                # [FIX] Preserve Specific Origin Tag (M15/M5/H1)
                                if origin_mark == "pivot_std":
                                    origin_mark = "pivot_1st"
                                self._log(f"[PIVOT] 1st Entry Logic OK ({origin_mark})", tag="PIVOT")
            else:
                # === Case 2: Add Position (Stream Rules) ===
                if getattr(self, "pivot_stream_enable", True):
                    # Must have position in same direction
                    count_same = sum(1 for p in poss if (p.type==mt5.POSITION_TYPE_BUY and target_side=="buy") or (p.type==mt5.POSITION_TYPE_SELL and target_side=="sell"))
                    # NANPIN中は同方向制限を解除して逆方向も許可（Pivot条件は同一）
                    allow_opposite_in_nanpin = bool(getattr(self, "_nanpin_lock", False))
                    if count_same > 0 or allow_opposite_in_nanpin:
                        is_entry_allowed = True
                        # [FIX] Preserve High Quality Origin Tags (M5/M15/H1)
                        # Only downgrade to 'pivot_stream' if currently 'pivot_std' (generic)
                        if origin_mark == "pivot_std":
                            origin_mark = "pivot_stream"
                        if not self.pivot_first_body_break_only_enable:
                            # ★ Pivot全体: 執行足の実体抜けを必須（追加エントリーも同条件）
                            exec_body_ok = True
                            bb_check_tf_stream = exec_s
                            if nanpin_override and bb_m1 is not None:
                                bb_check_tf_stream = "M1"
                            try:
                                _, _, _, _, bb_exec_stream = self._tf_dir(bb_check_tf_stream)
                                if target_side == "buy" and bb_exec_stream <= 0:
                                    exec_body_ok = False
                                    self._log(f"[GUARD] Stream blocked: waiting for {bb_check_tf_stream} body break BUY (bb={bb_exec_stream})", tag="PIVOT", level=1)
                                elif target_side == "sell" and bb_exec_stream >= 0:
                                    exec_body_ok = False
                                    self._log(f"[GUARD] Stream blocked: waiting for {bb_check_tf_stream} body break SELL (bb={bb_exec_stream})", tag="PIVOT", level=1)
                            except Exception as e:
                                self._log(f"[GUARD] Stream exec body break check error ({bb_check_tf_stream}): {e}", tag="PIVOT", level=2)
                            if not exec_body_ok:
                                is_entry_allowed = False
                            # ★ Pyramid Mode: require body break (bb>0/<0) on exec TF for adds
                            if getattr(self, "_is_pyramid_mode", False):
                                bb_entry = bb1
                                if nanpin_override and (bb_m1 is not None):
                                    bb_entry = bb_m1
                                if target_side == "buy" and (bb_entry is None or bb_entry <= 0):
                                    is_entry_allowed = False
                                    self._log(f"[PIVOT] Stream blocked (no body break {exec_s} BUY bb={bb_entry})", tag="PIVOT", level=1)
                                elif target_side == "sell" and (bb_entry is None or bb_entry >= 0):
                                    is_entry_allowed = False
                                    self._log(f"[PIVOT] Stream blocked (no body break {exec_s} SELL bb={bb_entry})", tag="PIVOT", level=1)

            # 3. Execution (Common Guard & Fire)
            if is_entry_allowed:
                 # Rescue & Distance Guard
                 is_rescue = False
                 dist_override = None
                 try:
                     req_side = target_side
                     if self._is_minority_rescue_applicable(req_side, poss):
                         is_rescue = True
                         dist_override = 0.2
                 except: pass

                 if not self._is_too_close_same_side(target_side, self.mid, origin="pivot", distance_mult_override=dist_override):
                     # CD Check
                     now_ts = time.time()
                     last_ts = float(getattr(self, "_last_pivot_fire_ts", 0.0))
                     
                     cd_val = float(cd)
                     # Anti-Frozen Logic (Reversal -> Shorten CD)
                     last_dir = getattr(self, "_last_pivot_dir_memory", 0)
                     curr_dir_val = 1 if target_side == "buy" else -1
                     
                     if last_dir != 0 and curr_dir_val != last_dir:
                          cd_val = float(getattr(self, "pivot_cooldown_sec", 60.0))
                          # Reset hot state
                          try:
                              hot_sec = float(getattr(self, "pivot_hot_sec", 10.0))
                              self._last_close_event_ts = time.time() - (hot_sec + 1.0)
                          except: pass

                     if (now_ts - last_ts) >= cd_val:
                          # Fire!
                          self._fire_pivot_ladder_guarded(
                              target_side,
                              origin_mark,
                              float(getattr(self, "base_lot", 0.01)),
                              exec_tf=exec_tf_logic,
                          )
                          self._last_pivot_fire_ts = now_ts
                          # Log pivot direction change for grid_mode=pivot_follow visibility
                          if curr_dir_val != last_dir:
                              direction_str = "BUY" if curr_dir_val == 1 else "SELL"
                              self._log(f"[PIVOT] Direction changed: {direction_str} (grid will follow if mode=pivot_follow)", level=1)
                          self._last_pivot_dir_memory = curr_dir_val


        # === ★ Pair-Net Entry (Generic for Exec TF) ===
        # Exec TF方向が切り替わった時のみ発火し、近い価格でのヘッジポジを提供
        if bool(getattr(self, "m1_pairnet_enable", M1_PAIRNET_ENABLE)):
            try:
                # [FIX] Use isolated helper for PairNet logic
                all_pos = self._get_my_positions()
                total_pnl = sum(float(getattr(p, "profit", 0.0)) for p in all_pos)
                if not bool(getattr(self, "_nanpin_lock", False)):
                    pass  # PairNetはNanpin中のみ有効
                elif len(all_pos) >= int(self.max_total_positions):
                    pass  # 上限到達 → スキップ
                elif len(all_pos) == 0:
                    pass  # ノーポジ時はスキップ（DD改善用のため）
                elif getattr(self, "_is_pyramid_mode", False) and (time.time() - float(getattr(self, "_last_pivot_fire_ts", 0.0))) < 2.0:
                    pass  # Guard: Pyramid Mode時、Pivot発火直後(2s)は重複防止のためスキップ
                elif len(all_pos) <= 1 and (time.time() - float(getattr(self, "_last_pivot_fire_ts", 0.0))) < 45.0:
                    pass  # Guard: Pivot 1st直後(45s)の重複エントリー防止
                else: 
                    # Determine dynamic variables based on Exec TF
                    # bb1 is already calculated from Exec TF in the Pivot Block above (line 4861 equivalent)
                    pairnet_d = 0
                    if exec_s == "M5":
                        pairnet_d = d5
                    elif exec_s == "M15":
                        pairnet_d = d60
                    else:
                        pairnet_d = d1
                    
                    prev_nd = 0
                    dist_ok = True
                    side_key = None
                    
                    if pairnet_d != 0:
                        prev_nd = getattr(self, "_last_pairnet_dir", 0)
                        side_key = "buy" if bb1 > 0 else "sell" if bb1 < 0 else None
                        
                        # ★v10.1: ヘッジ制限（パラメーター制御）
                        # M1_PAIRNET_HEDGE_LIMIT=True: 少数側のみヘッジ許可（差分まで）
                        if side_key and M1_PAIRNET_HEDGE_LIMIT:
                            n_buy = sum(1 for p in all_pos if p.type == mt5.POSITION_TYPE_BUY)
                            n_sell = sum(1 for p in all_pos if p.type == mt5.POSITION_TYPE_SELL)
                            
                            if side_key == "buy":
                                max_allowed = max(n_sell - n_buy, 2)
                                if n_buy >= max_allowed:
                                    side_key = None
                            elif side_key == "sell":
                                max_allowed = max(n_buy - n_sell, 2)
                                if n_sell >= max_allowed:
                                    side_key = None
                        
                        # 密度のチェック (50 points = 5 pips)
                        dist_ok = True
                        if side_key:
                            try:
                                # [FIX] Isolation
                                px = self._get_my_positions()
                                my_side = mt5.POSITION_TYPE_BUY if side_key == "buy" else mt5.POSITION_TYPE_SELL
                                current_tick = mt5.symbol_info_tick(self.symbol)
                                if current_tick:
                                    now_p = current_tick.ask if side_key == "buy" else current_tick.bid
                                    for p in px:
                                        if p.type == my_side:
                                            # 最小の間隔 (15 points = 保険用の最小ガード)
                                            if abs(p.price_open - now_p) < (15 * (mt5.symbol_info(self.symbol).point or 0.001)):
                                                dist_ok = False
                                                break
                            except: pass

                    # ★v10.1: dd1（強弱）フィルタ - 弱い実体抜けをブロック
                    dd1_ok = True
                    if side_key and M1_PAIRNET_DD1_FILTER:
                        try:
                            # dd1を取得（_tf_dirの2番目の戻り値）
                            # Refactored: Exec TF
                            tfs = exec_s
                            _, dd1, _, _, _ = self._tf_dir(tfs)
                            tick_check = mt5.symbol_info_tick(self.symbol)
                            if tick_check:
                                spread_pts = (tick_check.ask - tick_check.bid) / (mt5.symbol_info(self.symbol).point or 0.0001)
                                threshold = spread_pts * float(M1_PAIRNET_DD1_MULT)
                                if abs(dd1) <= threshold:
                                    dd1_ok = False
                                    self._log(f"[PAIRNET] dd1 filter blocked: dd1={dd1:.1f}, threshold={threshold:.1f} (spread={spread_pts:.1f})", level=2)
                        except Exception as e:
                            self._log(f"[PAIRNET] dd1 filter error: {e}", level=2)

                    # ★v10刷新: 安定レンジ抜けを信頼し、二段階トリガーを適用
                    should_fire = False
                    if bb1 != 0 and dd1_ok and self._bar_gate("pairnet_fire", exec_tf_logic):
                        if d1 != prev_nd: # 反転時は最優先
                            should_fire = True
                        elif dist_ok:     # 継続時も安定レンジを更新していれば距離ガード15ptで採用
                            should_fire = True

                    if should_fire and side_key:
                        if self._same_bar_same_side_dedupe_blocked(side_key, "pairnet", tf=exec_s):
                            should_fire = False

                    if should_fire and side_key:
                        side_m1 = side_key
                        sc_block_reason = self._sc_entry_block_reason(side_m1)
                        if sc_block_reason:
                            self._set_status(f"{sc_block_reason} (PairNet)")
                            self._log(f"[PAIRNET] blocked by Smart Close filter: {side_m1.upper()}", level=1)
                        else:
                            # Entry Budget無視でエントリー
                            base_lot = float(getattr(self, "lot", 0.01))
                            lots = self._norm_vol(base_lot * float(M1_PAIRNET_LOT_MULT))
                            
                            tick = mt5.symbol_info_tick(self.symbol)
                            if tick:
                                price = float(tick.ask if side_m1 == "buy" else tick.bid)
                                ord_type = mt5.ORDER_TYPE_BUY if side_m1 == "buy" else mt5.ORDER_TYPE_SELL
                                
                                ok_send = self._order_send_with_retry({
                                    "action":   mt5.TRADE_ACTION_DEAL,
                                    "symbol":   self.symbol,
                                    "volume":   lots,
                                    "type":     ord_type,
                                    "price":    round(price, self.digits),
                                    "deviation": DEVIATION,
                                    "magic":    self.magic,
                                    "comment":  "pairnet",
                                    "origin":   "pairnet",  # Entry Budget除外用
                                })
                                if ok_send:
                                    self._last_pairnet_dir = pairnet_d  # 方向を記録
                                    self._note_same_bar_entry(side_m1, "pairnet", tf=exec_s)
                                    # ★v10.1: GUI表示更新
                                    if not self.headless and getattr(self, "_mon_vars", None):
                                        fire_time = time.strftime("%H:%M:%S")
                                        try: self._safe_set(self._mon_vars["m1_pairnet_last"], f"{side_m1.upper()} {fire_time}")
                                        except: pass
            except Exception as e:
                            self._log(f"[M1-PAIRNET] error: {e}", level=1)


    def _refresh_pivot_ui(self):
        """発火から一定時間で表示を戻す（任意：30秒）"""
        if self.headless or not getattr(self, "_mon_vars", None):
            return
        if self._last_pivot_entries_n > 0 and (time.time() - self._last_pivot_entries_ts) > 30.0:
            self._last_pivot_entries_n = 0
            self._safe_set(self._mon_vars["pivot_last"], "0")
    def _is_tf_in_range(self, tf: str) -> bool:
        """
        前バー基準のレンジ判定:
        - st['is_range'] : 前バーで RH/RL をどちらも更新できていない → True
        - st['just_broke']: 今バーで RH 超え or RL 割れ が発生した → True

        レンジとしてブロックすべき条件 = 「前バーはレンジ」かつ「今バーでまだブレイクしていない」
        """
        st = getattr(self, "_pivot_state", {}).get(tf)
        if not st:
            return False
        is_range_prev = bool(st.get("is_range", False))    # 前バー基準
        just_broke    = bool(st.get("just_broke", False))  # 今バーでのブレイク
        return is_range_prev and (not just_broke)


    def _dir_to_icon(self, d: int) -> str:
        """+1/-1/0 を GUI 表示用テキストに変換"""
        return "↑(UP)" if d > 0 else ("↓(DOWN)" if d < 0 else "—")

    def _refresh_tf_dir_ui(self) -> None:
        """GUIのTF方向表示を更新 (Dynamic 5 Slots) + Dashboard Stats"""
        if self.headless or not getattr(self, "_mon_vars", None):
            return
        
        try:
            # 1. Update Timeframe Directions (5 Slots)
            slots = self._profile_tf_list if hasattr(self, "_profile_tf_list") else []

            # Nanpin時はM1プロファイルのTF表示に一時切替え
            nanpin_override = False
            m1_profile = None
            try:
                nanpin_override = (self._nanpin_lock and getattr(self.profile, "exec_tf", None) in (mt5.TIMEFRAME_M5, mt5.TIMEFRAME_M15))
                m1_profile = self.profiles.get("Scalp (M1)")
            except Exception:
                nanpin_override = False
                m1_profile = None

            if nanpin_override and m1_profile:
                slots = [
                    m1_profile.exec_tf,
                    m1_profile.c1_tf,
                    m1_profile.c2_tf,
                    m1_profile.ref1_tf,
                    m1_profile.ref2_tf
                ]
                tf_mode_str = f"TF Mode: {self.profile.name} → Scalp (M1) [Nanpin]"
            else:
                tf_mode_str = f"TF Mode: {self.profile.name}"
            self._safe_set(self._mon_vars.get("tf_mode"), tf_mode_str)
            
            for i, tf in enumerate(slots):
                # Layout uses dir_v1..dir_v5
                ui_key = f"dir_v{i+1}" 
                fallback_keys = ["dir_M1", "dir_M5", "dir_M15", "dir_H1", "dir_H4"] 
                
                target_var = None
                if ui_key in self._mon_vars:
                    target_var = self._mon_vars[ui_key]
                elif i < len(fallback_keys) and fallback_keys[i] in self._mon_vars:
                    target_var = self._mon_vars[fallback_keys[i]]
                
                if not target_var:
                    continue

                # Get Direction State (Direct Fetch)
                tf_str = self._get_tf_str(tf)
                # Label 更新（表示名を実際に見ているTFに合わせる）
                lbl_key = f"lbl_tf{i+1}"
                if lbl_key in self._mon_vars:
                    self._safe_set(self._mon_vars[lbl_key], f"{tf_str}:")
                d, rh, rl, ev, bb = self._tf_dir(tf_str)
                
                # Build Icon String
                icon = self._dir_to_icon(d)
                
                # Add Context Info from raw pivot state
                st = self._pivot_state.get(tf_str, {})
                mode = st.get("mode", "")
                ev_str = str(st.get("ev", ""))
                ovrd = (st.get("dir_override") is not None)

                if "両抜け" in ev_str: icon += " (Both)"
                elif mode == "range-inherit": icon += " (Range)"
                elif mode == "range-bar": icon += " (Break)"
                elif mode == "break-sync": icon += " (Sync)"
                
                if ovrd:
                    icon += " (Ovrd)"

                # Price Info
                if rh and rl:
                    icon += f" [H:{rh:.3f} L:{rl:.3f}]"

                # [FIX] Thread-safe update
                self._safe_set(target_var, icon)

            # 2. ★v10 Dashboard: 回転率の計算 (Turnover)
            it = self._internal_trading_time
            win = float(ENTRY_BUDGET_WINDOW_SEC)
            
            def get_stats(e_hist, c_hist):
                entries = len([t for t in e_hist if (it - t) <= win])
                closes  = len([t for t in c_hist if (it - t) <= win])
                rate = float(closes) / float(entries) if entries > 0 else 0.0
                return f"{rate:.2f} ({closes}/{entries})"

            self._safe_set(self._mon_vars["turnover_b"], get_stats(self._entry_hist_buy, self._close_hist_buy))
            self._safe_set(self._mon_vars["turnover_s"], get_stats(self._entry_hist_sell, self._close_hist_sell))

            # 3. ★v10 Dashboard: フラグ/ステータス
            flags = []
            try:
                # 少数側保護の判定
                # [FIX] Isolation
                positions = self._get_my_positions()
                n_buy  = sum(1 for p in positions if p.type == mt5.POSITION_TYPE_BUY)
                n_sell = sum(1 for p in positions if p.type == mt5.POSITION_TYPE_SELL)
                if abs(n_buy - n_sell) >= 2:
                    flags.append("🛡️Minority")
                # 利益温存の判定
                if PROFIT_PRESERVE_ENABLE:
                    preserved = self._get_preserve_tickets(positions)
                    if preserved:
                        flags.append(f"💎Preserve({len(preserved)})")
            except: pass
            
            self._safe_set(self._mon_vars["v10_flags"], " | ".join(flags) if flags else "—")

        except Exception as e:
            if "main thread is not in main loop" in str(e):
                pass
            else:
                self._log(f"[MON] refresh_tf_dir_ui error: {e}", tag="MON", level=2)


    def _enqueue_offset_retry(self, side: str, vol: float, comment: str = "offset-entry", origin: str = "offset") -> None:
        """最小距離ガードで弾いた成行きを後で再試行するための簡易キューに積む。"""
        try:
            # キュー肥大防止：50件上限
            if len(self._offset_retry_queue) >= 50:
                self._offset_retry_queue.pop(0)
            self._offset_retry_queue.append({
                "side": side,                 # 'buy' | 'sell'
                "vol": float(vol),
                "comment": comment,
                "origin": origin, 
                "ts": time.time(),            # 参考タイムスタンプ
                "grid_side": getattr(self, "_last_grid_side", None)  # 積んだ時点のWinside
            })
            # アンカー（Winside監視用）
            if self._offset_queue_anchor_side is None:
                self._offset_queue_anchor_side = getattr(self, "_last_grid_side", None)
        except Exception as e:
            self._log(f"[PAIRNET] enqueue error: {e}", tag="PAIRNET", level=2)

            
    def _process_offset_retry_queue(self) -> None:
        """キューの先頭を1件だけ再試行（毎ループ1件）。h1方向が入れ替わったらキューを選別クリア。"""
        try:
            if not self._offset_retry_queue:
                return
            cur_side = getattr(self, "_last_grid_side", None)
            # Winside入れ替わり検知 → 「新しい side 以外」を破棄（選別クリア）
            if self._offset_queue_anchor_side is not None and cur_side is not None:
                if cur_side != self._offset_queue_anchor_side:
                    before_n = len(self._offset_retry_queue)
                    self._offset_retry_queue = [
                        it for it in self._offset_retry_queue
                        if it.get("side") == cur_side
                    ]
                    self._offset_queue_anchor_side = cur_side
                    removed = before_n - len(self._offset_retry_queue)
                    self._log(f"[PAIRNET] offset-retry queue filtered on flip: kept side={cur_side}, removed={removed}",
                              tag="PAIRNET", level=1)
                    if not self._offset_retry_queue:
                        return

            # ▼ h1の有効方向（ロック後）を取得し、sideへ写像
            d15, *_ = self._tf_dir("M15")
            cur_side = "buy" if d15 > 0 else ("sell" if d15 < 0 else None)

            # h1が中立(0)のときは選別クリアを行わない（アンカー維持）
            if self._offset_queue_anchor_side is None:
                # アンカー未設定なら、h1がbuy/sellのときにだけ固定
                if cur_side is not None:
                    self._offset_queue_anchor_side = cur_side
            else:
                # h1の方向がアンカーと異なったら、h1の新方向以外を破棄（選別クリア）
                if cur_side is not None and cur_side != self._offset_queue_anchor_side:
                    before_n = len(self._offset_retry_queue)
                    self._offset_retry_queue = [
                        it for it in self._offset_retry_queue
                        if it.get("side") == cur_side
                    ]
                    self._offset_queue_anchor_side = cur_side
                    removed = before_n - len(self._offset_retry_queue)
                    self._log(
                        f"[OFFSET] retry-queue filtered on M15 flip: kept side={cur_side}, removed={removed}",
                        tag="OFFSET", level=1
                    )
                    if not self._offset_retry_queue:
                        return

            # ★v10.1: TTL超過アイテムを除去
            now_ts = time.time()
            ttl = float(OFFSET_RETRY_TTL_SEC)
            before_ttl = len(self._offset_retry_queue)
            self._offset_retry_queue = [
                it for it in self._offset_retry_queue
                if (now_ts - float(it.get("created", it.get("ts", now_ts)))) < ttl
            ]
            expired = before_ttl - len(self._offset_retry_queue)
            if expired > 0:
                self._log(f"[OFFSET] retry-queue TTL expired: removed={expired}", tag="OFFSET", level=1)
            if not self._offset_retry_queue:
                return

            # ── 先頭のみ処理（スパム防止）
            item = self._offset_retry_queue[0]
            side = item.get("side", "buy")
            vol  = float(item.get("vol", self.lot))
            n_remaining = int(item.get("n", 1))  # nパラメータ対応
            comment = item.get("comment", "offset-entry")
            origin  = item.get("origin", "offset")  # バケット区別
            is_hedged_offset = bool(item.get("is_hedged_offset", False)) # フラグ復元
            bypass_budget = bool(item.get("bypass_budget", True))

            tick = mt5.symbol_info_tick(self.symbol)
            if not tick:
                return
            price = tick.ask if side == "buy" else tick.bid
            
            # ▼▼▼ 片側チェック（重要バグ修正）▼▼▼
            # [FIX] Isolation
            poss = self._get_my_positions()
            buy_vol = sum(p.volume for p in poss if p.type == mt5.POSITION_TYPE_BUY)
            sell_vol = sum(p.volume for p in poss if p.type == mt5.POSITION_TYPE_SELL)
            
            if buy_vol > 0 and sell_vol == 0 and side == "buy":
                self._offset_retry_queue.pop(0)
                self._log("[OFFSET] retry skipped: one-sided (buy only)", tag="OFFSET", level=1)
                return
            if sell_vol > 0 and buy_vol == 0 and side == "sell":
                self._offset_retry_queue.pop(0)
                self._log("[OFFSET] retry skipped: one-sided (sell only)", tag="OFFSET", level=1)
                return
            # ▲▲▲ 片側チェック ▲▲▲
            
            # ▼▼▼ M5方向フィルタ ▼▼▼
            # M5の有効方向（ロック後）を取得し、"buy"/"sell"/None に写像
            d1, *_ = self._tf_dir("M5")
            M5_side = "buy" if d1 > 0 else ("sell" if d1 < 0 else None)

            # M5が中立(0)または、今回のアイテムsideと不一致なら"保留"（この周回は送らない）
            if (M5_side is None) or (M5_side != side):
                # 必要ならログだけ残す（スパム防止でレベル低め）
                self._log(f"[OFFSET] retry held: M5 side={M5_side}, item side={side}", tag="OFFSET", level=2)
                return
            # ▲▲▲ ここまで追加 ▲▲▲

            # まだ近すぎなら保留（origin='offset' バケットで判定）
            # if self._is_too_close_same_side(side, price, origin=origin):
            #     return

            sent = self._fire_offset_entries(
                side=str(side).lower(),
                vol=float(vol),
                n=1,
                origin=str(origin),
                comment=str(comment),
                is_hedged_offset=is_hedged_offset, # フラグ伝播
                bypass_budget=bypass_budget,  # ★v10.1: キューからフラグ復元
            )
            if sent > 0:
                # nパラメータ対応：1件成功でデクリメント
                n_remaining -= 1
                if n_remaining <= 0:
                    self._offset_retry_queue.pop(0)
                else:
                    self._offset_retry_queue[0]["n"] = n_remaining
                self._log(f"[OFFSET] offset-retry {side.upper()} filled @{price:.2f} (remaining={n_remaining})", tag="OFFSET", level=1)
            else:
                self._log(f"[OFFSET] offset-retry not sent (kept in queue)", tag="OFFSET", level=2)
                return

        except Exception as e:
            import traceback
            self._log(f"[OFFSET] retry process error: {e}\n{traceback.format_exc()}", tag="OFFSET", level=2)

    def _fire_offset_entries(self, side: str, vol: float, n: int = 1, origin: str = "offset", comment: str = "offset-entry", bypass_budget: bool = False, is_hedged_offset: bool = False) -> int:
        """
        相殺後の再構築（offset-entry）を安全に送る：
        - risk guard（DD/spread/rate/maxpos）を通す
        - entry budget にも加算する（close とは別）
          -> bypass_budget=Trueなら加算しない（Delta Maintain用）
        - 連発しすぎないように上限を掛ける
        """
        try:
            want = max(0, int(n))
        except Exception:
            want = 1
        if want <= 0:
            return 0
        max_n = int(getattr(self, "offset_rebuild_max_orders", OFFSET_REBUILD_MAX_ORDERS))
        if bool(getattr(self, "offset_rebuild_dynamic_limit", OFFSET_REBUILD_DYNAMIC_LIMIT)) and ENTRY_BUDGET_ENABLE:
            try:
                _ok, budget = self._entry_budget_check(side)
                _lim = budget.get("limit", 0)
                limit = int(_lim) if _lim is not None else 0
                
                _ew = budget.get("entries_win", 0)
                entries_win = int(_ew) if _ew is not None else 0
                
                protective = bool(budget.get("protective", False))
                if limit < 0:
                    dyn_cap = max_n
                else:
                    bonus = int(getattr(self, "offset_rebuild_budget_bonus", OFFSET_REBUILD_BUDGET_BONUS))
                    if protective:
                        bonus += int(getattr(self, "offset_rebuild_protective_bonus", OFFSET_REBUILD_PROTECTIVE_BONUS))
                    dyn_cap = max(0, (limit - entries_win) + bonus)
                if max_n > 0:
                    max_n = min(max_n, dyn_cap)
                else:
                    max_n = dyn_cap
            except Exception:
                pass
        if bool(getattr(self, "OFFSET_BALANCE_ONLY_IF_MINORITY_OR_EQUAL", OFFSET_BALANCE_ONLY_IF_MINORITY_OR_EQUAL)):
            try:
                # [FIX] Isolation
                poss = self._get_my_positions()
                buy_n = sum(1 for p in poss if int(getattr(p, "type", -1)) == int(mt5.POSITION_TYPE_BUY))
                sell_n = sum(1 for p in poss if int(getattr(p, "type", -1)) == int(mt5.POSITION_TYPE_SELL))
                side_l = str(side).lower()
                diff = abs(buy_n - sell_n) + 2
                if (side_l == "buy" and buy_n <= sell_n) or (side_l == "sell" and sell_n <= buy_n):
                    balance_cap = diff
                else:
                    balance_cap = 0
                if max_n > 0:
                    max_n = min(max_n, balance_cap)
                else:
                    max_n = balance_cap
            except Exception:
                pass
        # ▼ 片側残存時の本数抑制
        try:
            # [FIX] Isolation
            poss = self._get_my_positions()
            buy_vol = sum(p.volume for p in poss if p.type == mt5.POSITION_TYPE_BUY)
            sell_vol = sum(p.volume for p in poss if p.type == mt5.POSITION_TYPE_SELL)
            sell_vol = sum(p.volume for p in poss if p.type == mt5.POSITION_TYPE_SELL)
            is_one_sided = (buy_vol > 0 and sell_vol == 0) or (sell_vol > 0 and buy_vol == 0)
            
            # 異方向相殺(hedged offset)由来なら片側制限をスキップ
            if is_one_sided and not is_hedged_offset:
                cap = int(getattr(self, "offset_one_sided_cap", OFFSET_ONE_SIDED_CAP))
                if max_n > 0:
                    max_n = min(max_n, cap)
                else:
                    max_n = cap
                if n > max_n:
                     self._log(f"[OFFSET] One-sided CAP enforced: req={n} -> cap={max_n} (hedged={is_hedged_offset})", level=1)
        except Exception:
            pass
        # ▲ 片側残存時の本数抑制
        if max_n <= 0:
            return 0
        want = min(want, max_n)

        sent = 0
        for i in range(want):
            ok, why = self._risk_allows_new()
            if not ok:
                self._log(f"[OFFSET] rebuild blocked by risk guard: {why}", tag="OFFSET", level=2)
                break

            # buy/sellバランスが悪化する side の offset-entry は抑制
            if bool(getattr(self, "OFFSET_BALANCE_ONLY_IF_MINORITY_OR_EQUAL", OFFSET_BALANCE_ONLY_IF_MINORITY_OR_EQUAL)):
                try:
                    # [FIX] Isolation
                    poss = self._get_my_positions()
                    buy_n = sum(1 for p in poss if int(getattr(p, "type", -1)) == int(mt5.POSITION_TYPE_BUY))
                    sell_n = sum(1 for p in poss if int(getattr(p, "type", -1)) == int(mt5.POSITION_TYPE_SELL))
                    side_l = str(side).lower()
                    # “多数派側”への追加はブロック（同数なら許可）
                    if (side_l == "buy" and buy_n > sell_n) or (side_l == "sell" and sell_n > buy_n):
                        self._log(f"[OFFSET] rebuild blocked by balance (buy={buy_n}, sell={sell_n}, side={side_l})", tag="OFFSET", level=2)
                        break
                except Exception:
                    pass



            if self._send_entry_with_limit(
                side=side,
                vol=self._norm_vol(float(vol)),
                origin=origin,
                prefer_limit=False,
                comment=(comment if want == 1 else f"{comment}-{i+1}/{want}"),
            ):

                self._mark_offset_block()
                sent += 1

        if sent > 0:
            if bool(getattr(self, "OFFSET_COUNTS_TOWARD_ENTRY_BUDGET", OFFSET_COUNTS_TOWARD_ENTRY_BUDGET)) and not bypass_budget:
                self._note_entry_event(count=1)
            elif sent > 0 and bypass_budget:
                self._log(f"[BUDGET] Bypassed for offset entry (n={sent})", level=1)

        return sent


    # ── order wrappers ───────────────────────────────────────
    def _norm_vol(self, vol: float) -> float:
        info = mt5.symbol_info(self.symbol); step = info.volume_step or 0.01
        v = round(max(info.volume_min, min(vol, info.volume_max)) / step) * step
        return max(info.volume_min, min(v, info.volume_max))

    def _get_comm_pre(self, comment: str = "") -> str:
        """プロファイル名に基づいたコメント接頭辞 (v10.4)"""
        tag = self._get_inst_tag()
        return tag.strip("[]")

    def _send_entry_with_limit(self, side: str, vol: float, origin: str, price_hint: float | None = None,
                               prefer_limit: bool = False, comment: str | None = None) -> bool:
        """
        エントリー用の送信ヘルパ。prefer_limit=True なら指値を優先（スプレッドとオフセット条件付き）。
        指値条件を満たさなければ成行にフォールバックする。
        """
        tick = mt5.symbol_info_tick(self.symbol)
        info = mt5.symbol_info(self.symbol)
        if not tick or not info:
            return False
        pt = (getattr(info, "point", 0.0) or 0.0) if info else 0.0
        spread_pts = int(round(abs(tick.ask - tick.bid) / pt)) if pt else 0
        comment = comment or origin
        vol = self._norm_vol(vol)

        use_limit = prefer_limit and self.limit_entry_enable and pt > 0
        if use_limit and spread_pts <= self.limit_entry_max_spread_pts and self.limit_entry_offset_pts > 0:
            offset = self.limit_entry_offset_pts * pt
            price_lim = (tick.bid - offset) if side == "buy" else (tick.ask + offset)
            try:
                return bool(self._order_send_with_retry({
                    "action": mt5.TRADE_ACTION_PENDING,
                    "symbol": self.symbol,
                    "volume": vol,
                    "type": mt5.ORDER_TYPE_BUY_LIMIT if side == "buy" else mt5.ORDER_TYPE_SELL_LIMIT,
                    "price": round(price_lim, self.digits),
                    "sl": 0.0, "tp": 0.0, "deviation": DEVIATION, "magic": self.magic,
                    "comment": comment,
                    "type_time": getattr(mt5, "ORDER_TIME_GTC", None),
                }))
            except Exception:
                pass

        price_m = price_hint if price_hint is not None else (tick.ask if side == "buy" else tick.bid)
        try:
            return bool(self._order_send_with_retry({
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": self.symbol,
                "volume": vol,
                "type": mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
                "price": round(price_m, self.digits),
                "deviation": DEVIATION, "magic": self.magic,
                "comment": comment,
                "origin": origin,
            }))
        except Exception:
            return False

    def _market_close(self, pos, vol: float) -> bool:
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            return False
        ret = self._order_send_with_retry({
            "action": mt5.TRADE_ACTION_DEAL, "symbol": self.symbol,
            "position": pos.ticket, "volume": self._norm_vol(vol),
            "type": mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY,
            "price": tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask,
            "deviation": DEVIATION, "magic": self.magic, "comment": "pair-net close"
        })
        if ret:
            # Budget Count Update
            self._note_close_event("buy" if pos.type == mt5.POSITION_TYPE_BUY else "sell")
        return ret
        
    def _close_by(self, buy_ticket: int, sell_ticket: int) -> bool:
        ret = self._send_close_by(
            buy_ticket,
            sell_ticket,
            comment="pair-net close_by",
            deviation=DEVIATION,
            log_prefix="[CLOSEBY]",
        )
        if ret:
            # Budget Count Update (Both sides closed)
            self._note_close_event("buy")
            self._note_close_event("sell")
        return ret


    def _compute_step_pts(self, tick=None, info=None, mid_price: float | None = None) -> int:
        """
        グリッドのステップ幅（pts）を算出する。
        呼び出しは _compute_step_pts(tick, info) でも _compute_step_pts(mid_price=...) でも可。
        spread2/auto の“スプレッド×倍率(self.spread_mult)”に対応。
        """
        # --- シンボル情報 ---
        info = info or mt5.symbol_info(self.symbol)
        if not info:
            return 0
        pt = info.point
        spread_pts = int(info.spread) if info.spread is not None else 0

        # --- 現在価格（mid） ---
        if mid_price is None:
            if tick is None:
                tick = mt5.symbol_info_tick(self.symbol)
                if not tick:
                    return 0
            mid_price = (tick.bid + tick.ask) / 2.0

        mode = (self.step_mode or "spread2").lower()

        # helper: spread候補（倍率反映）
        spread_candidate = int(spread_pts * max(getattr(self, "spread_mult", 2.0), 1.0))

        if mode == "spread2":
            return max(spread_candidate, STEP_PTS_MIN_USER)

        if mode == "percent":
            return max(int(mid_price * STEP_PRICE_PCT / pt), STEP_PTS_MIN_USER)

        if mode == "abs_usd":
            return max(int(STEP_ABS_USD / pt), STEP_PTS_MIN_USER)

        if mode == "fixed_pts":
            return max(int(FIXED_STEP_PTS), STEP_PTS_MIN_USER)

        # auto: 候補の最小を採用（spread候補は倍率反映）
        if mode == "auto":
            cands = [
                spread_candidate,
                int(mid_price * STEP_PRICE_PCT / pt),
                int(STEP_ABS_USD / pt),
            ]
            if FIXED_STEP_PTS > 0:
                cands.append(int(FIXED_STEP_PTS))
            return max(min(cands), STEP_PTS_MIN_USER)

        # フォールバック
        return max(spread_candidate, STEP_PTS_MIN_USER)


    # ── slot helpers
    def _min_entry_distance(self) -> float:
        info = mt5.symbol_info(self.symbol)
        pt = (getattr(info, "point", 0.0) or 0.0) if info else 0.0
        try:
            return abs(int(getattr(self, "step_pts", 0) or 0)) * float(getattr(self, "min_entry_distance_mult", 0.6)) * pt
        except Exception:
            return 0.0

    def _is_too_close_same_side(
        self, side: str, price: float, pos_list=None, order_list=None, origin: str | None = None,
        distance_mult_override: float | None = None
    ) -> bool:
        """同一サイドの“直近エントリ付近”を避けるガード。
        origin が指定された場合はそのバケット内だけで判定（他originは無視）。
        """
        info = mt5.symbol_info(self.symbol)
        if not info: return False
        pt   = info.point or 0.0
        step = (self.step_pts or 0) * pt
        if distance_mult_override is not None:
             base_min = step * float(distance_mult_override)
        else:
             base_min = step * float(getattr(self, "min_entry_distance_mult", 1.0))

        # ★ 追加: スプレッド考慮（デフォ 0.5倍）
        tick = mt5.symbol_info_tick(self.symbol)
        spread = ((tick.ask - tick.bid) if (tick and pt) else 0.0)
        guard_spread_mult = float(getattr(self, "guard_spread_mult", 0.5))
        min_dist = base_min + spread * guard_spread_mult

        # origin 正規化（alias: recenter -> grid）
        bucket = self._origin_alias.get(origin, origin) if origin else None

        # 1) バケット内で“最後の成行エントリ価格”があればまずそれだけで判定
        if bucket and bucket in self._last_entry_by_origin:
            last = self._last_entry_by_origin[bucket].get(side)
            if last is not None and abs(price - float(last)) < min_dist:
                return True

        # 2) live positions / pending orders を同一バケットに絞ってスキャン
        def same_bucket(cmt: str) -> bool:
            c = (cmt or "").lower()
            if not bucket:
                return True  # 従来通り全体で判定
            if bucket == "grid":
                return c == (GRID_TAG or "").lower()
            if bucket == "tflow":
                return c.startswith("tflow-")
            if bucket == "mae":
                return c.startswith("mae-")
            if bucket == "offset":
                return c == "offset-entry"
            # --- pivot family: 独立バケット運用 ---
            if bucket and bucket.startswith("pivot"):
                # 例: bucket='pivot1h' → 'pivot1h-buy' などのコメントを同一バケット扱い
                return c.startswith(bucket + "-") or c == (bucket + "-entry")
            return True


        # [FIX] Isolation
        poss = list(pos_list) if pos_list is not None else self._get_my_positions()
        for p in poss:
            try:
                if p.type == (mt5.POSITION_TYPE_BUY if side == "buy" else mt5.POSITION_TYPE_SELL):
                    if same_bucket(getattr(p, "comment", "")):
                        if abs(price - float(getattr(p, "price_open", 0.0))) < min_dist:
                            return True
            except Exception:
                pass

        # [FIX] Isolation
        ords = list(order_list) if order_list is not None else self._get_my_orders()
        for o in ords:
            try:
                t = getattr(o, "type", None)
                is_buy  = t in (mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP, mt5.ORDER_TYPE_BUY_STOP_LIMIT)
                is_sell = t in (mt5.ORDER_TYPE_SELL, mt5.ORDER_TYPE_SELL_LIMIT, mt5.ORDER_TYPE_SELL_STOP, mt5.ORDER_TYPE_SELL_STOP_LIMIT)
                if (side == "buy" and is_buy) or (side == "sell" and is_sell):
                    if same_bucket(getattr(o, "comment", "")):
                        if abs(price - float(getattr(o, "price", 0.0))) < min_dist:
                            return True
            except Exception:
                pass

        return False

    # ────────────────────
    def _slot_index(self, side: int, price: float, mid: float, step_value: float, eps: float) -> int | None:
        """価格がどのスロットに属するか (1..self.side)。範囲外なら None"""
        if side == mt5.POSITION_TYPE_BUY:
            if price <= mid - eps: return None
            idx = round((price - mid) / step_value)
        else:  # SELL
            if price >= mid + eps: return None
            idx = round((mid - price) / step_value)
        if idx < 1 or idx > self.side:
            return None
        # アンカーとの距離がスロット幅/2を超える場合は無効（スロット境界外）
        anchor = (mid + idx * step_value) if side == mt5.POSITION_TYPE_BUY else (mid - idx * step_value)
        if abs(price - anchor) > (self.slot_width_mult * step_value) / 2 + eps:
            return None
        return int(idx)

    def _tp_for_price(self, price: float, side_str: str, pt: float) -> float:
        tick = mt5.symbol_info_tick(self.symbol)
        spread_pts = int(round((tick.ask - tick.bid) / pt)) if (tick and pt) else 0
        tp_pts_default = 2 * self.step_pts
        tp_pts_min = int(max(tp_pts_default, spread_pts * float(self.min_tp_spread_mult)))
        if side_str == "buy":
            return round(price + tp_pts_min * pt, self.digits)
        else:
            return round(price - tp_pts_min * pt, self.digits)

    def _tp_for_position(self, p) -> float:
        info = mt5.symbol_info(self.symbol); pt = info.point
        tick = mt5.symbol_info_tick(self.symbol)
        spread_pts = int(round((tick.ask - tick.bid) / pt)) if (tick and pt) else 0
        tp_pts_default = 2 * self.step_pts
        tp_pts_min = int(max(tp_pts_default, spread_pts * float(self.min_tp_spread_mult)))
        tgt = (p.price_open + tp_pts_min * pt
               if p.type == mt5.POSITION_TYPE_BUY
               else p.price_open - tp_pts_min * pt)
        return round(tgt, self.digits)


    def _tp_for_pending(self, order, info=None, tick=None):
        """
        Pending注文用のTP価格を返す。
        既存のデフォルトTP幅ロジック（ max( 2*step, spread*MIN_TP_SPREAD_MULT ) ）に準拠。
        BUY系 → base + width,  SELL系 → base - width
        """
        # シンボル情報
        info = info or mt5.symbol_info(self.symbol)
        if not info:
            return 0.0
        pt = info.point

        # step_pts が未セットの場合のフォールバック（spread×2）
        step_pts = self.step_pts or int(info.spread * 2)

        # 価格幅（price単位）
        width_pts = max(2 * step_pts, int(info.spread * MIN_TP_SPREAD_MULT))
        width = width_pts * pt

        base = getattr(order, "price_open", 0.0) or 0.0
        if order.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP):
            return base + width
        elif order.type in (mt5.ORDER_TYPE_SELL_LIMIT, mt5.ORDER_TYPE_SELL_STOP):
            return base - width
        else:
            return base  # 念のため（他タイプはそのまま）

    def _ema(self, arr, period):
        k = 2.0 / (period + 1.0)
        ema = None
        out = []
        for v in arr:
            ema = v if ema is None else (v - ema) * k + ema
            out.append(ema)
        return out

    def _atr(self, rates, period=14):
        if rates is None or len(rates) < period+1: return None
        trs = []
        prev_close = rates[0]['close']
        for r in rates[1:]:
            h,l,c = r['high'], r['low'], r['close']
            tr = max(h-l, abs(h-prev_close), abs(l-prev_close))
            trs.append(tr); prev_close = c
        if len(trs) < period: return None
        return sum(trs[-period:]) / period

    def _donch_hhv_llv(self, rates, n):
        if rates is None or len(rates) < n: return None, None
        highs = [r['high'] for r in rates[-n:]]
        lows  = [r['low']  for r in rates[-n:]]
        return max(highs), min(lows)

    def _tflow_signal(self):
        if not self.tflow_enable: 
            # self._tflow_dbg("off"); 
            return None
        if self._is_chop_blocked():
            return None
        now = time.time()
        if now - self._last_tflow_ts < self.tflow_cooldown: return None
        if not self.step_pts: return None

        info = self._get_cached_info(); tick = self._get_cached_tick()
        if not info or not tick: return None
        pt = info.point
        spread_pts = int(round((tick.ask - tick.bid)/pt)) if pt else 0
        if spread_pts <= 0: return None

        N = max(self.tflow_donch_n + self.tflow_ema_slow*3, 240)
        rates = mt5.copy_rates_from_pos(self.symbol, self.tflow_tf, 0, N)
        if rates is None or len(rates) < max(self.tflow_donch_n+5, self.tflow_ema_slow+10): return None
        closes = [r['close'] for r in rates]

        # EMA アライン
        ema_f = self._ema(closes, self.tflow_ema_fast)[-1]
        ema_s = self._ema(closes, self.tflow_ema_slow)[-1]
        trend_up, trend_dn = (ema_f > ema_s), (ema_f < ema_s)
        if not (trend_up or trend_dn): return None

        # ATR / Spread チェック
        atr = self._atr(rates, period=self.tflow_atr_period)
        if atr is None: return None
        atr_pts = int(round(atr/pt))
        if atr_pts < self.tflow_min_atr_spread * spread_pts:
            self._tflow_dbg(f"atr/spread low: {atr_pts}/{spread_pts}"); return None

        # Donchian ブレイク（+/- ATR*k でダマし抑制）
        hhv, llv = self._donch_hhv_llv(rates, self.tflow_donch_n)
        if hhv is None: return None
        mid_now = (tick.bid + tick.ask)/2.0
        up_trig = hhv + atr * self.tflow_break_atr_k
        dn_trig = llv - atr * self.tflow_break_atr_k

        if trend_up and mid_now >= up_trig:  return "buy"
        if trend_dn and mid_now <= dn_trig:  return "sell"
        # ★ フォールバック：ミニ・トレンド（頻度ブースト）
        step = (self.step_pts or 0) * pt
        if step > 0:
            # 直近のEMA_fastを“わずかに”超えて、かつ stepの30%分ブレイク
            if trend_up and mid_now >= ema_f + 0.30 * step: return "buy"
            if trend_dn and mid_now <= ema_f - 0.30 * step: return "sell"
        return None
        
    def _tflow_dbg(self, msg): self._log(msg, tag="TFLOW", level=2)

    def _tflow_fire(self, side: str) -> None:
        if self.trading_paused: return  # ★v10: 一時停止
        if self.running_tflow_close: return  # 決済中なら新規しない
        try:
            mid_now = float(self.mid) if self.mid is not None else 0.0
            if side == 'buy'  and self._is_too_close_same_side('buy',  mid_now, origin="tflow"):
                self._log(_t("log.guard.buy", price=mid_now, min=self._min_entry_distance()), tag="TFLOW", level=1)
                return False
            if side == 'sell' and self._is_too_close_same_side('sell', mid_now, origin="tflow"):
                self._log(_t("log.guard.sell", price=mid_now, min=self._min_entry_distance()), tag="TFLOW", level=1)
                return False
        except Exception:
            pass
        """Trend-Follow 成行エントリー（堅牢版）"""
        try:
            # ★v10.6: ナンピン防止ガード
            if self._is_nanpin_prevented(side) or self._is_majority_nanpin_filtered(side):
                self._set_status(f"TFlow skip nanpin/majority filter: {side}")
                return False

            ok, why = self._risk_allows_new()
            if not ok:
                self._set_status(_t("st.tflow.skip.risk", why=why))
                return False

            # 片側の同時本数制限
            # [FIX] Isolation
            poss = self._get_my_positions()
            max_live = int(getattr(self, "tflow_max_live", 3))
            if side == "buy":
                live = [p for p in poss if p.type == mt5.POSITION_TYPE_BUY  and (p.comment or "").startswith("tflow-")]
            else:
                live = [p for p in poss if p.type == mt5.POSITION_TYPE_SELL and (p.comment or "").startswith("tflow-")]
            if len(live) >= max_live:
                self._log(f"TFlow live-exceed: {len(live)}/{max_live}", tag="TFLOW", level=2)
                return False

            info = mt5.symbol_info(self.symbol); tick = mt5.symbol_info_tick(self.symbol)
            if not info or not tick:
                return False

            # ロット：最小量とステップに適合させる（10030対策）
            base = float(getattr(self, "lot", 0.01))
            mult = float(getattr(self, "tflow_lot_mult", 0.5))
            vol  = self._norm_vol(max(info.volume_min, base * mult))

            price = round(tick.ask if side == "buy" else tick.bid, self.digits)
            typ   = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL

            # 成行/指値エントリー（指値許可時は有利方向へ指値）
            self._send_entry_with_limit(
                side=side,
                vol=vol,
                origin="tflow",
                price_hint=price,
                prefer_limit=True,
                comment=f"tflow-{side}",
            )

            # 直後に出来た tflow-* ポジションを取得
            pnew = None
            # [FIX] Isolation
            for p in self._get_my_positions():
                if (p.comment or "").startswith("tflow-") and p.type == (mt5.POSITION_TYPE_BUY if side == "buy" else mt5.POSITION_TYPE_SELL):
                    pnew = p
                    break
            if not pnew:
                self._log(_t("log.tflow.no_new"), tag="TFLOW", level=2)
                return False

            # TP/SL 設定もラッパー経由で
            tp_price = self._tp_for_position(pnew)  # 既存TP幅ロジックを使用
            sl_price = 0.0
            if str(getattr(self, "tflow_sl_mode", "none")).lower() == "step" and getattr(self, "step_pts", 0):
                shift = int(round(self.step_pts * float(getattr(self, "tflow_sl_step_mult", 1.0)))) * info.point
                sl_price = round((pnew.price_open - shift) if side == "buy" else (pnew.price_open + shift), self.digits)

            self._order_send_with_retry({
                "action":    mt5.TRADE_ACTION_SLTP,
                "symbol":    self.symbol,
                "position":  pnew.ticket,
                "sl":        sl_price,
                "tp":        tp_price,
                "deviation": DEVIATION,
                "magic":     self.magic,
                "comment":   "tflow-set-sltp",
                "origin": "tflow",  # ★追加
            })

            self._last_tflow_ts = time.time()
            self._set_status(_t("st.tflow.fire", side=side.upper(), vol=vol))
            return True

        except Exception as e:
            self._log(_t("log.tflow.err", err=e), tag="TFLOW", level=1)
            return False



    # ── pending helpers ──────────────────────────────────────
    def _pend(self, ord_type: int, price: float, sl: float, tp: float = 0.0,
              vol: float | None = None, tag: str = GRID_TAG) -> None:
        if vol is None: vol = self.lot
        self._order_send_with_retry({
            "action": mt5.TRADE_ACTION_PENDING, "symbol": self.symbol,
            "volume": self._norm_vol(vol), "type": ord_type, "price": price,
            "sl": sl, "tp": tp, "deviation": DEVIATION, "magic": self.magic,
            "comment": tag, "type_time": mt5.ORDER_TIME_GTC,
        })

    def _desired_grid_prices(self, mid: float, pt: float):
        buys = [ round(mid + i * self.step_pts * pt, self.digits)  for i in range(1, self.side + 1) ]
        sells= [ round(mid - i * self.step_pts * pt, self.digits)  for i in range(1, self.side + 1) ]
        return buys, sells

    def _clear_all_grid_orders(self, reason: str = "cleanup") -> tuple[int, int, int]:
        """グリッド指値を全削除。
        Returns: (cleared_count, failed_count, rate_limited_count)
        """
        orders = self._get_my_orders()
        canceled_g = 0
        failed_g = 0
        skipped_rate_limit = 0

        for o in orders:
            # GRID_TAG = "recenter grid"
            # [FIX] 切り捨て対応: "[SCA]recenter gr" でも一致するように "recenter" で判定
            comment = (o.comment or "")
            if "recenter" in comment:
                # レート制限チェック
                if not self._rate_ok("orders", self.max_orders_per_min):
                    skipped_rate_limit += 1
                    continue

                try:
                    if hasattr(mt5, "order_delete"):
                        result = mt5.order_delete(o.ticket)
                        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                            canceled_g += 1
                        else:
                            failed_g += 1
                            self._log(f"[GRID] Order delete failed: ticket={o.ticket}, retcode={getattr(result, 'retcode', 'N/A')}", level=2)
                    else:
                        self._order_send_with_retry({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket, "symbol": o.symbol})
                        canceled_g += 1
                except Exception as e:
                    failed_g += 1
                    self._log(f"[GRID] Order delete exception: ticket={o.ticket}, error={e}", level=2)

        # ログ
        if canceled_g > 0 or failed_g > 0 or skipped_rate_limit > 0:
            msg = f"Grid cleared ({reason}): cleared={canceled_g}, failed={failed_g}, rate_limited={skipped_rate_limit}"
            self._set_status(msg)
            self._log(f"[GRID] {msg}", level=1)

        return canceled_g, failed_g, skipped_rate_limit

    def _decide_grid_side(self, pos_buys, pos_sells, long_profit, short_profit, mid_now: float):
        """Return (buy_allowed, sell_allowed) according to grid_mode and hysteresis."""
        mode = getattr(self, 'grid_mode', 'both')

        # [DEBUG] Log current grid mode once at startup
        if not hasattr(self, '_logged_grid_mode'):
            self._log(f"[GRID] Active grid_mode: {mode}", level=1)
            self._logged_grid_mode = True

        if mode == 'both':
            self._last_grid_side = None
            return True, True
        now = time.time()
        buy_allowed = sell_allowed = False
        want_side = None  # 'buy' or 'sell'
        if mode == 'winside_pnl':
            if long_profit > short_profit:
                want_side = 'buy'
            elif short_profit > long_profit:
                want_side = 'sell'
            else:
                return True, True
            if self.pnl_flip_abs_usd > 0.0:
                if abs(long_profit - short_profit) < self.pnl_flip_abs_usd and self._last_grid_side:
                    want_side = self._last_grid_side
        elif mode == 'minority_exposure':
            buy_vol  = sum(getattr(p, 'volume', 0.0) or 0.0 for p in pos_buys)
            sell_vol = sum(getattr(p, 'volume', 0.0) or 0.0 for p in pos_sells)
            if buy_vol == 0 and sell_vol == 0:
                return True, True
            if buy_vol <= sell_vol:
                want_side = 'buy'
            else:
                want_side = 'sell'
            top = max(buy_vol, sell_vol)
            if top > 0:
                ratio = abs(buy_vol - sell_vol) / top
                if ratio < self.exposure_flip_ratio and self._last_grid_side:
                    want_side = self._last_grid_side
        elif mode == 'pivot_follow':
            # [DEBUG] Log pivot_follow mode for verification
            if not hasattr(self, '_logged_pivot_follow_mode'):
                self._log(f"[GRID] MODE CONFIRMED: pivot_follow (pivot_dir={getattr(self, '_last_pivot_dir_memory', 0)})", level=1)
                self._logged_pivot_follow_mode = True

            # Pivotシグナルの最新方向（_last_pivot_dir_memory）に同期
            # ただし、現在のTF方向と整合性チェックを行う
            pivot_dir = getattr(self, "_last_pivot_dir_memory", 0)

            # [FIX] 現在のTF方向を取得して、Pivot方向が依然有効かチェック
            current_tf_dir = None
            try:
                # プロファイルのExec TFの現在方向を確認
                exec_tf_str = self._get_tf_str(self.profile.exec_tf)
                d_exec, *_ = self._tf_dir(exec_tf_str)
                current_tf_dir = d_exec  # +1, -1, or 0
            except Exception:
                pass

            # Pivot方向と現在のTF方向が一致する場合のみ従う
            if pivot_dir == 1:
                # Pivot記憶は買いだが、現在のTFも買い方向か確認
                if current_tf_dir is not None and current_tf_dir == 1:
                    want_side = 'buy'  # OK: 方向一致
                elif current_tf_dir == -1:
                    # 危険: Pivot記憶と現在方向が逆 → リセットして両側展開
                    self._log("[GRID] Pivot-Follow: Direction mismatch! Pivot=BUY but current TF=SELL. Resetting pivot memory and deploying both sides.", level=1)
                    self._last_pivot_dir_memory = 0  # 古いシグナルをリセット
                    return True, True
                else:
                    want_side = 'buy'  # TF=0 (range) なら記憶に従う
            elif pivot_dir == -1:
                # Pivot記憶は売りだが、現在のTFも売り方向か確認
                if current_tf_dir is not None and current_tf_dir == -1:
                    want_side = 'sell'  # OK: 方向一致
                elif current_tf_dir == 1:
                    # 危険: Pivot記憶と現在方向が逆 → リセットして両側展開
                    self._log("[GRID] Pivot-Follow: Direction mismatch! Pivot=SELL but current TF=BUY. Resetting pivot memory and deploying both sides.", level=1)
                    self._last_pivot_dir_memory = 0  # 古いシグナルをリセット
                    return True, True
                else:
                    want_side = 'sell'  # TF=0 (range) なら記憶に従う
            else:
                # Pivot信号なし(dir=0) → Nanpinガード風のロジックを適用
                # 両側にポジションがない場合は両側展開
                if not pos_buys or not pos_sells:
                    return True, True

                # ボリューム・PnLベースの危険判定（Nanpinガードと同じ）
                buy_vol = sum(p.volume for p in pos_buys)
                sell_vol = sum(p.volume for p in pos_sells)
                buy_pnl = long_profit
                sell_pnl = short_profit

                # 危険な偏り検出: 多数側が負け、少数側が勝ち → 少数側のみ展開
                if buy_vol > sell_vol and buy_pnl < 0 and sell_pnl > 0:
                    # BUY側が多数&負け、SELL側が勝ち → SELL側のみ
                    want_side = 'sell'
                elif sell_vol > buy_vol and sell_pnl < 0 and buy_pnl > 0:
                    # SELL側が多数&負け、BUY側が勝ち → BUY側のみ
                    want_side = 'buy'
                else:
                    # 安全な状態 → 両側展開
                    return True, True
        else:
            return True, True
        if self._last_grid_side and want_side != self._last_grid_side:
            if time.time() - self._last_grid_flip_ts < self.winside_flip_cooldown_sec:
                want_side = self._last_grid_side
        if want_side == 'buy':
            buy_allowed, sell_allowed = True, False
        elif want_side == 'sell':
            buy_allowed, sell_allowed = False, True
        else:
            buy_allowed = sell_allowed = True
        if want_side and want_side != self._last_grid_side:
            self._last_grid_side = want_side
            self._last_grid_flip_ts = time.time()
            # Log grid side change for visibility
            if mode == 'pivot_follow':
                self._log(f"[GRID] Pivot-Follow: Grid switched to {want_side.upper()} side (pivot_dir={getattr(self, '_last_pivot_dir_memory', 0)})", level=1)
        return buy_allowed, sell_allowed

    # ── grid build / recenter (slot-managed) ─────────────────
    def _build_or_repair_grid_slots(self, mid_now: float, initial: bool) -> None:
        """スロット管理で『空きスロットにだけ』未約定を配置。はみ出し/重複は少しずつ整理。"""
        print(f"[DEBUG] _build_or_repair_grid_slots: Starting (initial={initial}, _current_mode_str={getattr(self, '_current_mode_str', 'NOT SET')})")

        # [FIX] 全決済処理中はグリッド操作をスキップ
        if getattr(self, "_closing_in_progress", False):
            print(f"[DEBUG] _build_or_repair_grid_slots: Closing in progress, skipping grid operations.")
            return

        # [FIX] NANPIN(Recovery)ステータス時のみグリッドを稼働させる。
        # それ以外（IDLE, HOLD, PYRAMID等）では、グリッドの指値（recenter grid）を削除して終了。
        if self._current_mode_str != "NANPIN (Recovery)":
            if not initial:
                self._clear_all_grid_orders(reason="not in NANPIN mode")
            print(f"[DEBUG] _build_or_repair_grid_slots: Not in NANPIN mode, returning early.")
            return

        print(f"[DEBUG] _build_or_repair_grid_slots: In NANPIN mode or IDLE, continuing...")
        if self._is_chop_blocked():
            # 反転多発時は新規グリッドの拡大がDD要因になりやすいので、未約定を畳んで様子見
            try:
                if CHOP_CANCEL_PENDINGS:
                    canceled = self._cancel_pendings_quiet()
                else:
                    canceled = 0
                self._set_status(f"CHOP: grid paused (canceled={canceled})")
            except Exception:
                pass
            return
        print(f"[DEBUG] _build_or_repair_grid_slots: Past chop check, getting symbol info...")
        info = mt5.symbol_info(self.symbol); pt = info.point
        step_value = self.step_pts * pt
        eps = pt * PRICE_EPS_FACTOR

        want_buys, want_sells = self._desired_grid_prices(mid_now, pt)
        # 現在の未約定/ポジ
        # [FIX] Isolation
        cur_orders = self._get_my_orders()
        cur_buy_orders  = [o for o in cur_orders if o.type == mt5.ORDER_TYPE_BUY_STOP]
        cur_sell_orders = [o for o in cur_orders if o.type == mt5.ORDER_TYPE_SELL_STOP]
        # [FIX] Isolation
        cur_pos = self._get_my_positions()
        pos_buys  = [p for p in cur_pos if p.type == mt5.POSITION_TYPE_BUY]
        pos_sells = [p for p in cur_pos if p.type == mt5.POSITION_TYPE_SELL]

        # グリッド展開サイドの決定（モード + ヒステリシス）
        long_profit = sum(p.profit for p in pos_buys)
        short_profit = sum(p.profit for p in pos_sells)
        buy_allowed, sell_allowed = self._decide_grid_side(pos_buys, pos_sells, long_profit, short_profit, mid_now)

        # ★v10: pivot_follow時、M1 rh/rl で配置価格をフィルタ
        m1_rh_filter = None
        m1_rl_filter = None
        grid_mode = getattr(self, 'grid_mode', 'both')
        if grid_mode == 'pivot_follow':
            try:
                _, m1_rh, m1_rl, _, _ = self._tf_dir("M1")
                if m1_rh is not None:
                    m1_rh_filter = float(m1_rh)
                if m1_rl is not None:
                    m1_rl_filter = float(m1_rl)
            except Exception:
                pass

        # [DEBUG] Log grid deployment direction
        pivot_dir = getattr(self, '_last_pivot_dir_memory', 0)
        if buy_allowed and not sell_allowed:
            self._log(f"[GRID] Deploying BUY-side grid only (pivot_dir={pivot_dir})", level=1)
        elif sell_allowed and not buy_allowed:
            self._log(f"[GRID] Deploying SELL-side grid only (pivot_dir={pivot_dir})", level=1)
        elif buy_allowed and sell_allowed:
            self._log(f"[GRID] Deploying BOTH-side grid (pivot_dir={pivot_dir})", level=1)
        else:
            self._log(f"[GRID] Grid suppressed - no sides allowed (pivot_dir={pivot_dir})", level=1)

        # スロット占有マップ: index -> True（占有）
        occ_buy = {i: False for i in range(1, self.side+1)}
        occ_sell= {i: False for i in range(1, self.side+1)}

        # 既存ポジで占有（重なり防止の肝）
        for p in pos_buys:
            idx = self._slot_index(mt5.POSITION_TYPE_BUY, p.price_open, mid_now, step_value, eps)
            if idx: occ_buy[idx] = True
        for p in pos_sells:
            idx = self._slot_index(mt5.POSITION_TYPE_SELL, p.price_open, mid_now, step_value, eps)
            if idx: occ_sell[idx] = True

        # 既存未約定で占有 & 重複候補を分類
        dup_buy: dict[int, list] = defaultdict(list)
        dup_sell: dict[int, list] = defaultdict(list)
        out_of_range_orders = []

        for o in cur_buy_orders:
            idx = self._slot_index(mt5.POSITION_TYPE_BUY, o.price_open, mid_now, step_value, eps)
            if idx:
                dup_buy[idx].append(o); occ_buy[idx] = True
            else:
                out_of_range_orders.append(o)
        for o in cur_sell_orders:
            idx = self._slot_index(mt5.POSITION_TYPE_SELL, o.price_open, mid_now, step_value, eps)
            if idx:
                dup_sell[idx].append(o); occ_sell[idx] = True
            else:
                out_of_range_orders.append(o)

        # ① 空きスロットへ新規配置
        ok, why = self._risk_allows_new()
        created = 0
        if ok:
            # BUY
            for i, anchor in enumerate(want_buys, start=1):
                if buy_allowed and not occ_buy[i]:
                    # ★ pivot_follow: BUY STOPは1m rh以上にのみ配置
                    if m1_rh_filter is not None and anchor < m1_rh_filter:
                        continue

                    # 最小距離ガード（BUY）
                    if self._is_too_close_same_side('buy', anchor, pos_buys, cur_buy_orders, origin="grid"):
                        self._log(f"[GUARD] skip BUY @{anchor:.2f}: too close (min {self._min_entry_distance():.5f})", tag="GRID", level=1)
                        continue

                    tp = self._tp_for_price(anchor, "buy", pt)
                    # self._pend(mt5.ORDER_TYPE_BUY_STOP, anchor, sl=0.0, tp=tp)
                    self._pend(mt5.ORDER_TYPE_BUY_STOP, anchor, sl=0.0, tp=0.0)
                    occ_buy[i] = True; created += 1
            # SELL
            for i, anchor in enumerate(want_sells, start=1):
                if sell_allowed and not occ_sell[i]:
                    # ★ pivot_follow: SELL STOPは1m rl以下にのみ配置
                    if m1_rl_filter is not None and anchor > m1_rl_filter:
                        continue

                    # 最小距離ガード（SELL）
                    if self._is_too_close_same_side('sell', anchor, pos_sells, cur_sell_orders, origin="grid"):
                        self._log(f"[GUARD] skip SELL @{anchor:.2f}: too close (min {self._min_entry_distance():.5f})", tag="GRID", level=1)
                        continue

                    tp = self._tp_for_price(anchor, "sell", pt)
                    # self._pend(mt5.ORDER_TYPE_SELL_STOP, anchor, sl=0.0, tp=tp)
                    self._pend(mt5.ORDER_TYPE_SELL_STOP, anchor, sl=0.0, tp=0.0)
                    occ_sell[i] = True; created += 1
        else:
            self._set_status(f"Slots build skipped (risk guard: {why})")

        # ② はみ出し & 重複を少しずつ整理（負荷抑制）
        to_cancel = []
        # 範囲外
        to_cancel.extend(out_of_range_orders)
        # 重複(各スロット1本だけ残し、アンカーに近いものを残す)
        for idx, lst in dup_buy.items():
            if len(lst) > 1:
                anchor = want_buys[idx-1]
                lst.sort(key=lambda o: abs(o.price_open - anchor))
                to_cancel.extend(lst[1:])  # 近い1本を残し、残りをキャンセル
        for idx, lst in dup_sell.items():
            if len(lst) > 1:
                anchor = want_sells[idx-1]
                lst.sort(key=lambda o: abs(o.price_open - anchor))
                to_cancel.extend(lst[1:])

        # strict: disallowed side clean-up
        if getattr(self, 'strict_pending_cleanup', False):
            strict_cancel = []
            if not buy_allowed and cur_buy_orders:
                keep = int(getattr(self, 'keep_nearest_slots', 0))
                lst = sorted(cur_buy_orders, key=lambda o: abs(o.price_open - mid_now))
                strict_cancel.extend(lst[keep:])
            if not sell_allowed and cur_sell_orders:
                keep = int(getattr(self, 'keep_nearest_slots', 0))
                lst = sorted(cur_sell_orders, key=lambda o: abs(o.price_open - mid_now))
                strict_cancel.extend(lst[keep:])
            to_cancel.extend(strict_cancel)
        # 上限までだけキャンセル
        canceled = 0
        for o in to_cancel[: self.slot_cleanup_per_recenter]:
            if hasattr(mt5, "order_delete"):
                mt5.order_delete(o.ticket)
            else:
                self._order_send_with_retry({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket, "symbol": o.symbol})
            canceled += 1

        msg_kind = "Initial build" if initial else "Recenter slots"
        print(f"[DEBUG] _build_or_repair_grid_slots: Completed. Created={created}, Canceled={canceled}")
        self._set_status(f"{msg_kind}: add {created}, clean {canceled} (limit {self.slot_cleanup_per_recenter})")

    # ── grid creation ────────────────────────────────────────
    def _build_grid(self) -> None:
        print("[DEBUG] _build_grid: Getting tick/info...")
        tick = mt5.symbol_info_tick(self.symbol); info = mt5.symbol_info(self.symbol)
        print(f"[DEBUG] _build_grid: Got tick, computing mid and step_pts...")
        self.mid      = round((tick.bid + tick.ask) / 2, self.digits)
        self.step_pts = self._compute_step_pts(tick, info)
        print(f"[DEBUG] _build_grid: Calling _build_or_repair_grid_slots (mid={self.mid}, step_pts={self.step_pts})...")
        self._build_or_repair_grid_slots(self.mid, initial=True)
        print("[DEBUG] _build_grid: Completed successfully.")

    # ── recenter: slot-based incremental ─────────────────────
    def _recenter(self, mid_now: float) -> None:
        if self.recompute_step_on_recenter:
            tick = mt5.symbol_info_tick(self.symbol); info = mt5.symbol_info(self.symbol)
            self.step_pts = self._compute_step_pts(tick, info)
        self.mid = round(mid_now, self.digits)
        self._build_or_repair_grid_slots(self.mid, initial=False)

    # ── TP arming helpers ────────────────────────────────────
    def _arm_tp(self, winners: list, reason: str) -> None:
        if not winners: return
        armed_new = []
        for p in winners:
            if reason not in self._armed_reasons[p.ticket]:
                if not self._rate_ok("orders", self.max_orders_per_min):
                    self._set_status("ARM skipped (rate limit)")
                    break
                self._order_send_with_retry({
                    "action": mt5.TRADE_ACTION_SLTP, "symbol": self.symbol,
                    "position": p.ticket, "sl": 0.0, "tp": 0.0, "deviation": DEVIATION
                })
                self._armed_reasons[p.ticket].add(reason)
                armed_new.append(p.ticket)
        if armed_new:
            self._log(f"ARM[{reason}] winners: {armed_new}")

    def _disarm_tp_by_reason(self, reason: str) -> None:
        if not self._armed_reasons: return
        tickets = [t for t, reasons in self._armed_reasons.items() if reason in reasons]
        if not tickets: return
        for t in tickets:
            reasons = self._armed_reasons.get(t, set())
            if reason in reasons:
                reasons.remove(reason)
            if not reasons:
                # [FIX] Isolation
                for p in self._get_my_positions():
                    if p.ticket == t:
                        tp = self._tp_for_position(p)
                        self._order_send_with_retry({
                            "action": mt5.TRADE_ACTION_SLTP, "symbol": self.symbol,
                            "position": t, "sl": 0.0, "tp": tp, "deviation": DEVIATION
                        })
                        break
                self._armed_reasons.pop(t, None)
        if tickets:
            self._log(f"DISARM[{reason}] restored: {tickets}")

    def _disarm_all(self) -> None:
        all_tickets = list(self._armed_reasons.keys())
        for t in all_tickets:
            # [FIX] Isolation
            for p in self._get_my_positions():
                if p.ticket == t:
                    tp = self._tp_for_position(p)
                    self._order_send_with_retry({
                        "action": mt5.TRADE_ACTION_SLTP, "symbol": self.symbol,
                        "position": t, "sl": 0.0, "tp": tp, "deviation": DEVIATION
                    })
                    break
        if all_tickets:
            self._log(f"DISARM[all] restored: {all_tickets}")
        self._armed_reasons.clear()

    def _tp_control(self) -> None:
        """TP管理（TP_MAX_LOSERS=0で実質無効化）"""
        now = time.time()
        if TP_MAX_LOSERS <= 0:
            return
        if now - self._last_tp_ctrl_ts < TP_CTRL_COOLDOWN_SEC:
            return
        self._last_tp_ctrl_ts = now

        # [FIX] Isolation
        positions = self._get_cached_positions()
        orders    = self._get_cached_orders()
        if not positions and not orders:
            return

        info = self._get_cached_info()
        tick = self._get_cached_tick()
        if not info or not tick:
            return

        # --- ロング・ショートの合計損益を算出（当該シンボルのみ） ---
        long_profit  = sum(p.profit for p in positions if p.type == mt5.POSITION_TYPE_BUY)
        short_profit = sum(p.profit for p in positions if p.type == mt5.POSITION_TYPE_SELL)

        # --- 負け側の方向を決定（引き分けなら何もしない） ---
        if long_profit < short_profit:
            losing_side = mt5.POSITION_TYPE_BUY
        elif short_profit < long_profit:
            losing_side = mt5.POSITION_TYPE_SELL
        else:
            return

        # =====================================================================================
        # 成行ポジション: 勝ち側→TP外す / 負け側→“建値が不利”な最悪N本だけTP付ける（残りは外す）
        # =====================================================================================

        # 勝ち側（losing_side ではない側）のTPは外す
        for p in positions:
            if p.type != losing_side:
                has_tp = (getattr(p, "tp", 0.0) or 0.0) != 0.0
                if has_tp:
                    self._order_send_with_retry({
                        "action": mt5.TRADE_ACTION_SLTP,
                        "position": p.ticket, "symbol": self.symbol,
                        "sl": getattr(p, "sl", 0.0) or 0.0,
                        "tp": 0.0,
                        "deviation": DEVIATION, "magic": self.magic,
                        "comment": "tpctrl-remove-win"
                    })
                    # self._log(f"Removed TP from profitable pos {p.ticket}", tag="TPCTRL")

        # 負け側のみ抽出し、“建値が不利”順にソート
        losers = [p for p in positions if p.type == losing_side]
        if losing_side == mt5.POSITION_TYPE_BUY:
            # BUYの負け側: 高値掴みほど不利 → price_open 降順
            losers_sorted = sorted(losers, key=lambda p: p.price_open, reverse=True)
        else:
            # SELLの負け側: 安値掴みほど不利 → price_open 昇順
            losers_sorted = sorted(losers, key=lambda p: p.price_open, reverse=False)

        # 最悪N本にはTPを付与、N超はTPを外す（資源として保持）
        for idx, p in enumerate(losers_sorted):
            has_tp = (getattr(p, "tp", 0.0) or 0.0) != 0.0
            if idx < TP_MAX_LOSERS:
                if not has_tp:
                    tp_price = self._tp_for_position(p)  # 既定の幅: max(2*step, spread*MIN_TP_SPREAD_MULT)
                    self._order_send_with_retry({
                        "action": mt5.TRADE_ACTION_SLTP,
                        "position": p.ticket, "symbol": self.symbol,
                        "sl": getattr(p, "sl", 0.0) or 0.0,
                        "tp": tp_price,
                        "deviation": DEVIATION, "magic": self.magic,
                        "comment": "tpctrl-set-lose"
                    })
                    # self._log(f"Set TP on losing pos {p.ticket} at {tp_price}", tag="TPCTRL")
            else:
                if has_tp:
                    self._order_send_with_retry({
                        "action": mt5.TRADE_ACTION_SLTP,
                        "position": p.ticket, "symbol": self.symbol,
                        "sl": getattr(p, "sl", 0.0) or 0.0,
                        "tp": 0.0,
                        "deviation": DEVIATION, "magic": self.magic,
                        "comment": "tpctrl-remove-extra"
                    })
                    # self._log(f"Removed TP from extra losing pos {p.ticket}", tag="TPCTRL")

        # =====================================================================================
        # 未約定注文（pending）：勝ち側→TP外す / 負け側→“建値が不利”な最悪N本だけTP付ける
        # =====================================================================================

        # 勝ち側 pending は TP を外す
        for o in orders:
            if o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP):
                order_side = mt5.POSITION_TYPE_BUY
            elif o.type in (mt5.ORDER_TYPE_SELL_LIMIT, mt5.ORDER_TYPE_SELL_STOP):
                order_side = mt5.POSITION_TYPE_SELL
            else:
                continue

            has_tp = (getattr(o, "tp", 0.0) or 0.0) != 0.0

            if order_side != losing_side:
                if has_tp:
                    mt5.order_send({
                        "action": mt5.TRADE_ACTION_MODIFY,
                        "order": o.ticket, "symbol": self.symbol,
                        "price": o.price_open,
                        "sl": o.sl, "tp": 0.0
                    })
                    # self._log(f"Removed TP from winning pending {o.ticket}", tag="TPCTRL")

        # 負け側 pending のみ抽出し、“建値が不利”順にソート
        loser_orders = []
        for o in orders:
            if o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP) and losing_side == mt5.POSITION_TYPE_BUY:
                loser_orders.append(o)
            elif o.type in (mt5.ORDER_TYPE_SELL_LIMIT, mt5.ORDER_TYPE_SELL_STOP) and losing_side == mt5.POSITION_TYPE_SELL:
                loser_orders.append(o)

        if losing_side == mt5.POSITION_TYPE_BUY:
            loser_orders_sorted = sorted(loser_orders, key=lambda o: o.price_open, reverse=True)
        else:
            loser_orders_sorted = sorted(loser_orders, key=lambda o: o.price_open, reverse=False)

        # 最悪N本だけTP付与、それ以外はTP外す
        for idx, o in enumerate(loser_orders_sorted):
            has_tp = (getattr(o, "tp", 0.0) or 0.0) != 0.0
            if idx < TP_MAX_LOSERS:
                if not has_tp:
                    tp_price = self._tp_for_pending(o, info, tick)
                    mt5.order_send({
                        "action": mt5.TRADE_ACTION_MODIFY,
                        "order": o.ticket, "symbol": self.symbol,
                        "price": o.price_open,
                        "sl": o.sl, "tp": tp_price
                    })
                    # self._log(f"Set TP on losing pending {o.ticket} at {tp_price}", tag="TPCTRL")
            else:
                if has_tp:
                    mt5.order_send({
                        "action": mt5.TRADE_ACTION_MODIFY,
                        "order": o.ticket, "symbol": self.symbol,
                        "price": o.price_open,
                        "sl": o.sl, "tp": 0.0
                    })
                    # self._log(f"Removed TP from extra losing pending {o.ticket}", tag="TPCTRL")

    def _cancel_pendings_only(self) -> int:
        n = self._cancel_pendings_quiet()
        try:
            if not self.headless and getattr(self, "root", None):
                self.root.update_idletasks()
        except Exception:
            pass
        self._set_status(f"Canceled {n} pending orders.")
        return n

    def _minority_side(self) -> str | None:
        """現在のポジション露出の少数側を返す ('buy'/'sell'/None)"""
        # [FIX] Isolation
        poss = self._get_my_positions()
        if not poss:
            return None
        expo_buy  = sum(p.volume for p in poss if p.type == mt5.POSITION_TYPE_BUY)
        expo_sell = sum(p.volume for p in poss if p.type == mt5.POSITION_TYPE_SELL)
        if expo_buy == expo_sell:
            return None
        return "buy" if expo_buy < expo_sell else "sell"

    def _cancel_and_exit(self) -> None:
        """GUI用：未約定だけキャンセルして安全終了。現在のシンボル限定。"""
        try:
            # 任意：確認ダイアログ（不要ならこの if ブロックを削ってOK）
            if messagebox.askyesno("Confirm",
                                f"Cancel ALL pending orders for {self.symbol} and exit?",
                                parent=self.root) is False:
                return
            self.running = False  # 監視ループ停止
            # Save State on Exit
            self._save_offset_state_to_disk()
            n = self._cancel_pendings_only()
            try:
                messagebox.showinfo("Done", f"Canceled {n} pending orders. Exiting.", parent=self.root)
            except Exception:
                pass
        finally:
            try: mt5.shutdown()
            except: pass
            # GUIを閉じる
            if getattr(self, "root", None):
                self.root.after(300, self.root.quit)


    # ── stuck-aware: 事前アーミング（多数側＝担がれ側のみ監視） ─────────────────
    def _stuck_arm_if_needed(self, positions: list, tick, info, loser_side: int, winner_side: int) -> None:
        if not self.stuck_enable:
            return
        now = time.time()
        if now - self._last_stuck_arm_ts < self.stuck_arm_cooldown:
            return
        self._last_stuck_arm_ts = now

        pt = info.point
        spread_pts = int(round((tick.ask - tick.bid) / pt)) if (tick and pt) else 0
        adverse_thr = int(self.stuck_adverse_mult * spread_pts)

        losers = []
        for p in positions:
            if p.type != loser_side:  # 多数側のみ評価
                continue
            if p.type == mt5.POSITION_TYPE_BUY:
                adverse_pts = int(round(max(0.0, (p.price_open - tick.bid) / pt)))
            else:
                adverse_pts = int(round(max(0.0, (tick.ask - p.price_open) / pt)))
            if p.profit < 0 and adverse_pts >= adverse_thr:
                losers.append((adverse_pts, p))

        if len(losers) >= self.stuck_min_losers:
            winners = [p for p in positions if p.type == winner_side and p.profit > 0]
            winners.sort(key=lambda x: x.profit, reverse=True)
            winners = winners[: self.stuck_arm_max_winners]
            if winners:
                self._arm_tp(winners, reason="stuck")
        else:
            self._disarm_tp_by_reason("stuck")

    # ── v10.4: Profile Change Handler ───────────────────────────
    def _save_profile_state_named(self, name: str):
        """指定したプロファイル名を永続化ファイルに保存する"""
        try:
            path = _get_profile_path(self.symbol)
            data = {"profile": name, "updated_at": time.time()}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception as e:
            self._log(f"[Persistent] Failed to save profile state: {e}", level=2)

    def _update_gui_labels(self):
        """Update GUI labels based on current profile"""
        if self.headless or not getattr(self, "_mon_vars", None): return

        # Dynamic Logic: Map 5 slots (Exec, C1, C2, Ref1, Ref2)
        slots = [
            self.profile.exec_tf,
            self.profile.c1_tf,
            self.profile.c2_tf, 
            self.profile.ref1_tf,
            self.profile.ref2_tf
        ]
        self._profile_tf_list = slots
        self._tf_dir_cache = {}  # Clear cache on profile switch

        # Update Labels
        for i, tf_const in enumerate(slots):
            lbl_key = f"lbl_tf{i+1}"
            tf_name = self._get_tf_str(tf_const)
            if lbl_key in self._mon_vars:
                    self._safe_set(self._mon_vars[lbl_key], f"{tf_name}:")

    def _on_profile_change(self, event=None):
        name = self.profile_var.get()

        # [FIX] Startup Guard: If name matches initial/current, do nothing
        if name == getattr(self, "_last_profile_name", None):
             return
        
        # Special case: If switching to "Unified" while enabled, just suppress popup BUT update labels
        if name == "Unified (M1+M5+M15)" and getattr(self, "gui_parent", None):
            self._update_gui_labels() # Ensure labels match actual profile (e.g. M5)
            self._last_profile_name = name
            return

        self._last_profile_name = name # update last seen

        if name in self.profiles:
            # [FIX] Separation Restart: If in Unified Mode and switching to Single, MUST restart
            if getattr(self, "gui_parent", None):
                if messagebox.askyesno("再起動確認", "Unified Mode (タブ動作) から単独モードへの切り替えには再起動が必要です。今すぐ再起動しますか？"):
                    self._save_profile_state_named(name)
                    self._save_profile_state_named(name)
                    # Restart process
                    import sys
                    if "--auto" in sys.argv:
                        self._log("[RESTART] Auto-mode separation. Exiting to let batch loop restart.", level=1)
                        os._exit(0)
                    else:
                        python = sys.executable
                        os.execl(python, python, *sys.argv)
                else:
                    # Revert selection if user says No (optional, or just ignore)
                    return

            old = self.profile.name
            if name == old: return # Redundant check
            self.profile = self.profiles[name]
            self._log(f"[PROFILE] Switched from {old} to {self.profile.name}", level=1)
            self._set_status(f"Profile: {self.profile.name}")
            
            # Save Persistence
            self._save_profile_state()
            
            # [FIX] Apply Profile Params (CD / Hold)
            if hasattr(self.profile, "cd_sec"):
                self.pivot_cooldown_sec = float(self.profile.cd_sec)
            if hasattr(self.profile, "hold_sec"):
                self.term_min_hold_sec = float(self.profile.hold_sec)
            if hasattr(self.profile, "term_target_profit_usd") and self.profile.term_target_profit_usd is not None:
                self.term_target_profit_usd = float(self.profile.term_target_profit_usd)
            
            # GUI Labels Update
            self._update_gui_labels()

        elif name == "Unified (M1+M5+M15)":
            # Smart Restart implementation
            if messagebox.askyesno("再起動確認", "Unified Mode (統合スキャル) への切り替えには再起動が必要です。今すぐ再起動しますか？"):
                self._save_profile_state_named(name)
                # Restart process
                import sys
                if "--auto" in sys.argv:
                    # Let batch loop handle it to avoid duplicate processes
                    self._log("[RESTART] Auto-mode detected. Exiting to let batch loop restart.", level=1)
                    # wait slightly for log to flush
                    # messagebox is already modal, so we just exit here
                    os._exit(0)
                else:
                    python = sys.executable
                    os.execl(python, python, *sys.argv)
            else:
                # Revert
                self.profile_var.set(self.profile.name)
        elif name == "Smart (M5+M15)":
            # [FIX] Separation Restart: If in Unified Mode and switching to Single/Smart, MUST restart
            if getattr(self, "gui_parent", None):
                if messagebox.askyesno("再起動確認", "Unified Mode (タブ動作) からSmart Modeへの切り替えには再起動が必要です。今すぐ再起動しますか？"):
                    self._save_profile_state_named(name)
                    # Restart process
                    import sys
                    if "--auto" in sys.argv:
                        self._log("[RESTART] Auto-mode separation. Exiting to let batch loop restart.", level=1)
                        os._exit(0)
                    else:
                        python = sys.executable
                        os.execl(python, python, *sys.argv)
                else:
                    return

            # [NEW] Activate Smart Profile Mode
            self._smart_profile_enable = True
            self._smart_state = "STANDBY"
            self._smart_base_profile = None
            
            # Default to "Day (M5)" for standby monitoring, but keep Smart flag
            target = "Day (M5)"
            if target in self.profiles:
                self.profile = self.profiles[target]
                self._log(f"[SMART] Mode Enabled. Standby on {target}", level=1)
                self._set_status(f"Smart: Standby ({target})")
                
                # Apply params
                if hasattr(self.profile, "cd_sec"): self.pivot_cooldown_sec = float(self.profile.cd_sec)
                if hasattr(self.profile, "hold_sec"): self.term_min_hold_sec = float(self.profile.hold_sec)
                
                self._update_gui_labels()
                self._save_profile_state_named(name) # Save as "Smart..."
        else:
            # If switching away from Smart/Unified to standard
            self._smart_profile_enable = False
            self._smart_state = "IDLE"

    # ── internal helper: 確定バー判定 (Generic) ────────────────
    def _check_new_bar(self, tf, tick=None) -> bool:
        """
        指定TFで新しい足が始まったかチェック（簡易版）
        ★v10.5: 起動直後は次の足まで待つ（即検出防止）
        """
        try:
            if tick is None:
                tick = mt5.symbol_info_tick(self.symbol)
            if not tick: return False
            # M1=60, M5=300...
            sec_map = {
                mt5.TIMEFRAME_M1: 60, mt5.TIMEFRAME_M5: 300, mt5.TIMEFRAME_M15: 900,
                mt5.TIMEFRAME_H1: 3600, mt5.TIMEFRAME_H4: 14400, mt5.TIMEFRAME_D1: 86400
            }
            interval = sec_map.get(tf, 60)
            
            # Use integer division of server timestamp
            curr_idx = int(tick.time) // interval
            attr_name = f"_last_bar_idx_{interval}"
            last_idx = getattr(self, attr_name, 0)
            
            # [FIX] 起動直後（last_idx=0）は次の足まで待つ
            if last_idx == 0:
                setattr(self, attr_name, curr_idx)
                return False  # 初回は次の足まで待つ
            
            if curr_idx > last_idx:
                setattr(self, attr_name, curr_idx)
                return True
            return False
        except:
            return False

    def _get_tf_str(self, tf_const: int) -> str:
        """MT5 timeframe constant to string (e.g. 1 -> 'M1')"""
        tf_str_map = {
            mt5.TIMEFRAME_M1: "M1", mt5.TIMEFRAME_M5: "M5", mt5.TIMEFRAME_M15: "M15",
            mt5.TIMEFRAME_H1: "H1", mt5.TIMEFRAME_H4: "H4", mt5.TIMEFRAME_D1: "D1", mt5.TIMEFRAME_W1: "W1"
        }
        return tf_str_map.get(tf_const, "M1")

    # ── pair-net 本体（多数側＝担がれ側優先） ──────────────────────────────
    def _close_positions(self, plist: list, is_offset: bool = False) -> int:
        """
        安全なクローズ with Boss-Last順序:
        - Winner（多数派）を先に成行決済
        - Boss（少数派の最悪損失）を最後に決済
        - 中断時にBossが残ることで、次のTickで相殺再開可能
        ★v10機能3: 決済前にリアルタイムnet profitをチェック、閾値割れなら中断
        """
        if is_offset:
            self._closing_in_progress = True
            self._closing_reason = "offset_closing"

        try:
            if not plist:
                return 0

            # BossとWinnerを分類
            buys  = [p for p in plist if p.type == mt5.POSITION_TYPE_BUY]
            sells = [p for p in plist if p.type == mt5.POSITION_TYPE_SELL]
            
            # ★v10.2: 既に閉じたticketを除外
            closed_tickets = self._offset_state.get("closed_tickets", set())
            buys = [p for p in buys if p.ticket not in closed_tickets]
            sells = [p for p in sells if p.ticket not in closed_tickets]
            
            # ★v10.2: 前回の確定利益を引き継ぎ (Offsetモードのみ)
            realized_profit_approx = self._offset_state.get("realized_profit", 0.0) if is_offset else 0.0
            
            # どちらが少数派（Boss候補）かを判定
            if len(buys) > len(sells):
                winners = buys   # 多数派
                bosses = sells   # 少数派（Boss）
                boss_side = "sell"
                winner_side = "buy"
            elif len(sells) > len(buys):
                winners = sells
                bosses = buys
                boss_side = "buy"
                winner_side = "sell"
            else:
                # 同数の場合、利益で判定（利益が低い方がBoss側）
                sum_buy = sum(p.profit for p in buys)
                sum_sell = sum(p.profit for p in sells)
                if sum_buy < sum_sell:
                    winners = sells
                    bosses = buys
                    boss_side = "buy"
                    winner_side = "sell"
                else:
                    winners = buys
                    bosses = sells
                    boss_side = "sell"
                    winner_side = "buy"
            
            # ★v10.3: 利益確保の厳格化
            # Winnersリストの中に「含み損」が含まれていると、それを先に決済して中断した場合に確定損になる。
            # そのため、Winnersの中の「負けポジ」はBossesリスト（後回し）へ移動させる。
            # これにより、常に「利食い」→「損切り」の順序を強制する。
            
            real_winners = []
            for w in winners:
                if w.profit > 0:
                    real_winners.append(w)
                else:
                    bosses.append(w)
            winners = real_winners
            
            # 利益順（降順）にソート（大きい利益から確保）
            winners.sort(key=lambda p: p.profit, reverse=True)
            
            # ★v10.2: 状態更新（進行中）
            if is_offset:
                self._offset_state["active"] = True
                self._offset_state["last_boss_side"] = boss_side
            
            closed = 0
            target_tickets = {p.ticket for p in (buys + sells)}

            # ─────────────────────────────────────────────────────────
            # [STEP 1] Winnerを先に決済（最後の1つを残す = close_by用）
            # ─────────────────────────────────────────────────────────
            for i, w in enumerate(winners):
                # 最後の1つはclose_by用に残す（Bossがいる場合のみ）
                if i == len(winners) - 1 and len(bosses) > 0:
                    break
                
                # 逆行チェック (Boss含む全体Netの悪化を監視)
                # ★v10.3: 判定復活 + 緩和策
                # 相殺セット完了までの間に、スプレッド等で一時的に0を下回っても、
                # 致命的な損失(-2.0以上)でなければ続行させる。
                if CLOSE_ABORT_ON_REVERSAL and closed > 0:
                    try:
                        # [FIX] Isolation
                        fresh_poss = self._get_my_positions()
                        remaining = [p for p in fresh_poss if p.ticket in target_tickets]
                        current_net = sum(p.profit for p in remaining)
                        total_pnl_estimate = current_net + realized_profit_approx
                        
                        # 閾値はバッファなし（マイナス容認しない）
                        abort_thresh = float(CLOSE_MIN_NET_THRESHOLD)
                        
                        if total_pnl_estimate < abort_thresh:
                            self._log(f"[ABORT] Net profit dropped to {total_pnl_estimate:.2f} (< {abort_thresh}), aborting remaining closes", level=1)
                            return closed
                    except Exception:
                        pass
                
                if self._market_close(w, w.volume):
                    closed += 1
                    target_tickets.discard(w.ticket)
                    realized_profit_approx += getattr(w, "profit", 0.0)
                    
                    # ★v10.2: 状態更新
                    if is_offset:
                        self._offset_state["closed_tickets"].add(w.ticket)
                        self._offset_state["realized_profit"] = realized_profit_approx
                        if winner_side == "buy":
                            self._offset_state["winner_count_buy"] += 1
                        else:
                            self._offset_state["winner_count_sell"] += 1
                        
                        # ★v10.2: GUI更新
                        if not self.headless and getattr(self, "_mon_vars", None):
                            try:
                                wc = self._offset_state["winner_count_buy"] + self._offset_state["winner_count_sell"]
                                profit = self._offset_state["realized_profit"]
                                self._safe_set(self._mon_vars["offset_state"], f"W:{wc} ${profit:.1f}")
                            except: pass
            
            # ─────────────────────────────────────────────────────────
            # [STEP 2] 最後のWinner + Bossをペア決済（close_by優先）
            # ─────────────────────────────────────────────────────────
            
            boss_next_idx = 0
            if len(bosses) > 0 and len(winners) > 0:
                last_winner = winners[-1]
                boss = bosses[0]  # Boss側は1つだけの想定（相殺ロジック上）
                boss_next_idx = 1 # STEP 2で消費したので次から
                
                # 量がズレていたら先に調整
                if abs(last_winner.volume - boss.volume) > 1e-9:
                    if last_winner.volume > boss.volume:
                        self._market_close(last_winner, max(0.0, last_winner.volume - boss.volume))
                    else:
                        self._market_close(boss, max(0.0, boss.volume - last_winner.volume))
                
                # ★v10.3 Fix: Boss決済直前のABORT判定
                if CLOSE_ABORT_ON_REVERSAL and closed > 0:
                    try:
                        # [FIX] Isolation
                        fresh_poss = self._get_my_positions()
                        # 残りのターゲット（LastWinner + Boss）
                        remaining = [p for p in fresh_poss if p.ticket in target_tickets]
                        current_net = sum(p.profit for p in remaining)
                        total_pnl_estimate = current_net + realized_profit_approx
                        
                        # 閾値を下回っていたら、Bossを切らずに終了する（利益確保して撤退）
                        if total_pnl_estimate < float(CLOSE_MIN_NET_THRESHOLD):
                            self._log(f"[ABORT] Pre-Boss check: Net dropped to {total_pnl_estimate:.2f} (Threshold={CLOSE_MIN_NET_THRESHOLD}). Aborting Boss close.", level=1)
                            return closed
                    except Exception:
                        pass

                # close_byを試行
                ok = False
                if getattr(self, "use_close_by", True):
                    ok = self._close_by(last_winner.ticket, boss.ticket)
                
                if ok:
                    closed += 2
                    target_tickets.discard(last_winner.ticket)
                    target_tickets.discard(boss.ticket)
                    realized_profit_approx += getattr(last_winner, "profit", 0.0) + getattr(boss, "profit", 0.0)
                    
                    # ★v10.2: 状態更新（close_by成功）
                    if is_offset:
                        self._offset_state["closed_tickets"].add(last_winner.ticket)
                        self._offset_state["closed_tickets"].add(boss.ticket)
                        self._offset_state["realized_profit"] = realized_profit_approx
                        if winner_side == "buy":
                            self._offset_state["winner_count_buy"] += 1
                        else:
                            self._offset_state["winner_count_sell"] += 1
                else:
                    # フォールバック: Winner → Bossの順で成行
                    if self._market_close(last_winner, last_winner.volume):
                        closed += 1
                        target_tickets.discard(last_winner.ticket)
                        realized_profit_approx += getattr(last_winner, "profit", 0.0)
                        
                        # ★v10.2: 状態更新
                        if is_offset:
                            self._offset_state["closed_tickets"].add(last_winner.ticket)
                            self._offset_state["realized_profit"] = realized_profit_approx
                            if winner_side == "buy":
                                self._offset_state["winner_count_buy"] += 1
                            else:
                                self._offset_state["winner_count_sell"] += 1
                    
                    if self._market_close(boss, boss.volume):
                        closed += 1
                        target_tickets.discard(boss.ticket)
                        realized_profit_approx += getattr(boss, "profit", 0.0)
                        if is_offset:
                            self._offset_state["closed_tickets"].add(boss.ticket)
                            self._offset_state["realized_profit"] = realized_profit_approx
            
            # 残りのBossがあれば処理（通常は1つだが念のため）
            # ★v10.3 Fix: Step 2がスキップされた場合でも残りのBossを処理する
            for b in bosses[boss_next_idx:]:
                if CLOSE_ABORT_ON_REVERSAL and closed > 0:
                    try:
                        # [FIX] Isolation
                        fresh_poss = self._get_my_positions()
                        remaining = [p for p in fresh_poss if p.ticket in target_tickets]
                        current_net = sum(p.profit for p in remaining)
                        total_pnl_estimate = current_net + realized_profit_approx
                        if total_pnl_estimate < float(CLOSE_MIN_NET_THRESHOLD):
                            self._log(f"[ABORT] Remaining Boss check: Net dropped to {total_pnl_estimate:.2f}, aborting", level=1)
                            return closed
                    except Exception:
                        pass
                if self._market_close(b, b.volume):
                    closed += 1
                    target_tickets.discard(b.ticket)
                    realized_profit_approx += getattr(b, "profit", 0.0)
                    if is_offset:
                        self._offset_state["closed_tickets"].add(b.ticket)
                        self._offset_state["realized_profit"] = realized_profit_approx
            
            # ★v10.2: Boss退治完了 → 状態に最終Winner側とカウントを保存（re-entry計算用）
            # 注: 実際のリセットは_process_offset_tx完了後に行う
            if is_offset:
                self._offset_state["final_winner_side"] = winner_side
                self._offset_state["final_winner_count"] = (
                    self._offset_state["winner_count_buy"] if winner_side == "buy"
                    else self._offset_state["winner_count_sell"]
                )

            # ★v10.3: 決済があったら状態を永続化（利益データの保護） (Offsetのみ)
            if closed > 0 and is_offset:
                self._save_offset_state_to_disk()

            return closed
        finally:
            if is_offset:
                self._closing_in_progress = False
                self._closing_reason = ""

    # =====================================================================
    # §SC: Smart Close — Pool/Fixed管理・4段階決済・状態評価
    # =====================================================================

    def _sc_parse_target_magics(self) -> None:
        """Parse Smart Close target magic list and always include this bot's magic."""
        magics: set[int] = set()
        raw = str(getattr(self, "smart_target_magic_numbers", "") or "")
        for part in raw.split(","):
            tok = part.strip()
            if not tok:
                continue
            try:
                magics.add(int(tok))
            except Exception:
                continue
        try:
            magics.add(int(getattr(self, "magic", 0)))
        except Exception:
            pass
        self._sc_target_magics = magics

    def _sc_is_target_magic(self, magic: int) -> bool:
        try:
            if not self._sc_target_magics:
                self._sc_parse_target_magics()
            return int(magic) in self._sc_target_magics
        except Exception:
            return False

    def _sc_get_positions(self) -> list:
        """Smart Close position source: symbol + target magics (ignore Ignore-Magic toggle)."""
        with _MT5_LOCK:
            poss = mt5.positions_get(symbol=self.symbol) or []
        out = []
        for p in poss:
            try:
                if self._sc_is_target_magic(int(getattr(p, "magic", 0))):
                    out.append(p)
            except Exception:
                continue
        # prune commission cache when positions shrink
        if len(self._sc_comm_cache) > max(10, len(out) * 2):
            self._sc_comm_cache = {}
        return out

    def _sc_get_position_commission(self, ticket: int) -> float:
        """Sum commission for a position ticket from history (cached)."""
        try:
            t = int(ticket)
        except Exception:
            return 0.0
        if t in self._sc_comm_cache:
            return float(self._sc_comm_cache[t])
        comm = 0.0
        try:
            deals = mt5.history_deals_get(position=t)
            if deals:
                for d in deals:
                    comm += float(getattr(d, "commission", 0.0) or 0.0)
        except Exception:
            comm = 0.0
        self._sc_comm_cache[t] = comm
        return comm

    def _sc_effective_profit(self, pos) -> float:
        """MT5ポジションの実効損益 (profit + swap + commission)."""
        return (
            float(getattr(pos, "profit", 0.0) or 0.0)
            + float(getattr(pos, "swap", 0.0) or 0.0)
            + self._sc_get_position_commission(int(getattr(pos, "ticket", 0) or 0))
        )

    def _sc_update_spread(self) -> None:
        """Update spread history for moving average window."""
        import time
        now = time.time()
        info = mt5.symbol_info(self.symbol)
        if info:
            self._sc_spread_history.append((float(info.spread), now))
        cutoff = now - self._sc_spread_window_sec
        self._sc_spread_history = [(s, t) for s, t in self._sc_spread_history if t >= cutoff]

    def _sc_avg_spread(self) -> float:
        """スプレッド移動平均 (points)。履歴なければ symbol_info.spread を直接返す"""
        if self._sc_spread_history:
            return sum(s for s, _ in self._sc_spread_history) / len(self._sc_spread_history)
        info = mt5.symbol_info(self.symbol)
        return float(info.spread) if info else 0.0

    def _sc_is_cent_account(self) -> bool:
        """Heuristic cent-account detection from account fields."""
        try:
            sym = str(getattr(self, "symbol", "") or "").strip().lower()
            acc = mt5.account_info()
            currency = str(getattr(acc, "currency", "") or "").strip().lower() if acc else ""
            server = str(getattr(acc, "server", "") or "").strip().lower() if acc else ""
            name = str(getattr(acc, "name", "") or "").strip().lower() if acc else ""
            company = str(getattr(acc, "company", "") or "").strip().lower() if acc else ""
            text = " ".join([currency, server, name, company])
            is_exness = ("exness" in text)

            # Exness cent symbols are typically suffixed with "c" (e.g. XAUUSDc).
            # Restrict this rule to Exness contexts to avoid false positives.
            if is_exness and (sym == "xauusdc" or sym.endswith("c")):
                return True

            if "cent" in text:
                return True
            # Common cent currency codes: USC, EURC, GBPC, etc.
            if currency.endswith("c") and len(currency) in (4, 5):
                return True
            return False
        except Exception:
            return False

    def _sc_target_scale(self) -> float:
        """Return target multiplier (1.0 normally, cent multiplier on cent accounts)."""
        if self._sc_target_scale_cache is not None:
            return float(self._sc_target_scale_cache)
        scale = 1.0
        try:
            if bool(SMART_TARGET_AUTO_SCALE_CENT):
                if self._sc_is_cent_account():
                    scale = float(SMART_TARGET_CENT_MULTIPLIER)
        except Exception:
            scale = 1.0
        self._sc_target_scale_cache = float(scale)
        return float(scale)

    def _sc_target_parameter_effective(self) -> float:
        """
        Spread target の有効倍率。
        Recovery は 5x を基準にし、HOLD/PYRAMID では少し重くする。
        """
        try:
            base = float(getattr(self, "smart_target_parameter", SMART_TARGET_PARAMETER))
        except Exception:
            base = float(SMART_TARGET_PARAMETER)

        try:
            if not bool(globals().get("SMART_TARGET_DYNAMIC_MODE_ENABLE", SMART_TARGET_DYNAMIC_MODE_ENABLE)):
                return float(base)
        except Exception:
            return float(base)

        mode = str(getattr(self, "_current_mode_str", "") or "").lower()
        try:
            if "pyramid" in mode:
                return float(globals().get("SMART_TARGET_PARAMETER_PYRAMID", SMART_TARGET_PARAMETER_PYRAMID))
            if "hold" in mode:
                return float(globals().get("SMART_TARGET_PARAMETER_HOLD", SMART_TARGET_PARAMETER_HOLD))
            if "nanpin" in mode or "recovery" in mode:
                return float(globals().get("SMART_TARGET_PARAMETER_RECOVERY", SMART_TARGET_PARAMETER_RECOVERY))
        except Exception:
            pass
        return float(base)

    def _sc_log_boot_once(self) -> None:
        """Log Smart Close runtime config once after startup."""
        if bool(getattr(self, "_sc_boot_log_done", False)):
            return
        try:
            scale = self._sc_target_scale()
            cent = self._sc_is_cent_account()
            eff = self._sc_target_parameter_effective()
            self._log(
                f"[SC][BOOT] mode={self.smart_target_mode} param={self.smart_target_parameter} eff={eff:.2f} "
                f"scale={scale:.2f} cent={cent} symbol={self.symbol} caution={self.smart_caution_mode} "
                f"target_magics={sorted(self._sc_target_magics)}",
                tag="SC",
                level=1,
            )
        except Exception:
            pass
        self._sc_boot_log_done = True

    def _sc_distribute_profit(self, pnl: float) -> None:
        """決済損益を Pool と Fixed に振り分ける"""
        if pnl > 0:
            to_pool = 0.0
            to_fixed = 0.0
            recover_need = max(0.0, -float(getattr(self, "_sc_pool", 0.0)))
            if recover_need > 0.0:
                recovered = min(float(pnl), recover_need)
                to_pool += recovered
                remain = float(pnl) - recovered
                if remain > 0.0:
                    extra_pool = remain * self.smart_profit_usage_rate / 100.0
                    to_pool += extra_pool
                    to_fixed += remain - extra_pool
                self._log(
                    f"[POOL_RECOVERY] Profit prioritized to pool: +{recovered:.2f} "
                    f"(remain={remain:.2f})",
                    tag="SC",
                    level=1,
                )
            else:
                to_pool = pnl * self.smart_profit_usage_rate / 100.0
                to_fixed = pnl - to_pool
            self._sc_pool  += to_pool
            self._sc_fixed += to_fixed
            self._log(
                f"[SC] Profit distributed: total={pnl:.2f} "
                f"pool+={to_pool:.2f}(bal={self._sc_pool:.2f}) "
                f"fixed+={to_fixed:.2f}(bal={self._sc_fixed:.2f})",
                tag="SC", level=1
            )
        elif pnl < 0:
            self._sc_pool += pnl          # 損失は Pool から補填
            self._log(f"[SC] Loss deducted from pool: {pnl:.2f}, pool={self._sc_pool:.2f}", tag="SC", level=1)

    def _sc_reset_negative_pool_after_compensation(self, reason: str) -> None:
        """補填系の決済後に Pool が負なら Fixed へ振り替えて Pool をゼロ化する。"""
        try:
            pool_before = float(getattr(self, "_sc_pool", 0.0) or 0.0)
            if pool_before >= 0.0:
                return
            fixed_before = float(getattr(self, "_sc_fixed", 0.0) or 0.0)
            deficit = abs(pool_before)
            self._sc_fixed += pool_before
            self._sc_pool = 0.0
            self._log(
                f"[POOL_RESET] {reason}: pool ${pool_before:.2f} -> $0.00, "
                f"fixed ${fixed_before:.2f} -> ${self._sc_fixed:.2f} (offset=${deficit:.2f})",
                tag="SC",
                level=1,
            )
        except Exception:
            pass

    def _sc_close_pos(self, pos) -> bool:
        """Close single position using Smart Close deviation."""
        try:
            tick = mt5.symbol_info_tick(self.symbol)
            if not tick:
                return False
            ptype = int(getattr(pos, "type", -1))
            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": self.symbol,
                "position": int(getattr(pos, "ticket", 0)),
                "volume": self._norm_vol(float(getattr(pos, "volume", 0.0))),
                "type": mt5.ORDER_TYPE_SELL if ptype == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY,
                "price": float(tick.bid if ptype == mt5.POSITION_TYPE_BUY else tick.ask),
                "deviation": int(getattr(self, "smart_sc_deviation", SMART_SC_DEVIATION)),
                "magic": int(getattr(self, "magic", 0)),
                "comment": "smart-close",
            }
            ok = bool(self._order_send_with_retry(req))
            if ok:
                self._note_close_event("buy" if ptype == mt5.POSITION_TYPE_BUY else "sell")
            return ok
        except Exception:
            return False

    def _sc_close_pos_async(self, pos) -> bool:
        """Async-style sender for Smart Close phase 2."""
        return self._sc_close_pos(pos)

    def _sc_can_use_close_by(self) -> bool:
        try:
            info = mt5.symbol_info(self.symbol)
            mode = int(getattr(info, "order_mode", 0)) if info else 0
            closeby_flag = int(getattr(mt5, "SYMBOL_ORDER_CLOSEBY", 0))
            supported = bool(closeby_flag and (mode & closeby_flag))
            self._sc_close_by_supported = supported
            if bool(getattr(self, "use_close_by", True)) and not supported:
                self._log(
                    f"[SC][CLOSEBY] symbol_info unsupported/unknown: order_mode={mode} flag={closeby_flag}; try anyway",
                    tag="SC",
                    level=1,
                )
        except Exception:
            self._sc_close_by_supported = False
        return bool(getattr(self, "use_close_by", True))

    def _sc_close_by_async(self, buy_ticket: int, sell_ticket: int) -> bool:
        try:
            ok = self._send_close_by(
                int(buy_ticket),
                int(sell_ticket),
                comment="smart-close-by",
                deviation=int(getattr(self, "smart_sc_deviation", SMART_SC_DEVIATION)),
                log_prefix="[SC][CLOSEBY]",
            )
            if ok:
                self._note_close_event("buy")
                self._note_close_event("sell")
            return ok
        except Exception:
            return False

    def _sc_threshold(self, side_positions: list, avg_spread: float) -> float:
        """
        CanAddPosition/Offset判定に使うスプレッドベースの閾値 (USD)
        side_positions: 対象サイドのポジションリスト
        """
        info = mt5.symbol_info(self.symbol)
        if not info or avg_spread <= 0:
            return 999999.0
        pt = float(info.point)
        cs = float(info.trade_contract_size)
        return sum(p.volume * avg_spread * 2.0 * pt * cs for p in side_positions)

    def _sc_can_add_position(self, side: str, buys: list, sells: list, avg_spread: float) -> bool:
        """
        CanAddPosition (SmartCloser §8): 追加エントリー可否を判定
        side: "buy" or "sell"
        """
        side_pos  = buys  if side == "buy"  else sells
        opp_pos   = sells if side == "buy"  else buys
        side_cnt  = len(side_pos)
        opp_cnt   = len(opp_pos)

        if side_cnt == 0 or opp_cnt == 0:
            return True

        side_pnl  = sum(self._sc_effective_profit(p) for p in side_pos)
        opp_pnl   = sum(self._sc_effective_profit(p) for p in opp_pos)
        threshold = self._sc_threshold(side_pos, avg_spread)

        # Cond 2: 損失＆少数派 → 平均コスト下げ許可
        if side_pnl <= -threshold and side_cnt < opp_cnt:
            return True
        # Cond 3: ヘッジ条件
        if side_cnt < opp_cnt and side_pnl > 0 and opp_pnl < 0:
            if side_pnl < abs(opp_pnl):
                return True
        # Cond 4: ピラミッディング条件
        if side_pnl >= threshold:
            if side_cnt >= opp_cnt:
                profitable = sum(1 for p in side_pos if self._sc_effective_profit(p) >= 0)
                if profitable < side_cnt / 2.0:
                    return False
            return True
        return False

    def _sc_is_profit_threshold_entry(self, side: str, buys: list, sells: list, avg_spread: float) -> bool:
        """Cond 4 (Profit Threshold) のみ評価。IsEntryRestricted 内で使用"""
        side_pos  = buys  if side == "buy"  else sells
        opp_pos   = sells if side == "buy"  else buys
        side_cnt  = len(side_pos)
        opp_cnt   = len(opp_pos)
        if side_cnt == 0:
            return False
        side_pnl  = sum(self._sc_effective_profit(p) for p in side_pos)
        threshold = self._sc_threshold(side_pos, avg_spread)
        if side_pnl >= threshold:
            if side_cnt >= opp_cnt:
                profitable = sum(1 for p in side_pos if self._sc_effective_profit(p) >= 0)
                return profitable >= side_cnt / 2.0
            return True
        return False

    def _sc_is_entry_restricted(self, side: str, buys: list, sells: list, avg_spread: float) -> bool:
        """IsEntryRestricted (SmartCloser §9): restriction状態に基づくエントリー制限"""
        r = self._sc_restriction
        if r == 0:  # RESTRICT_NONE
            return False
        if r == 3:  # RESTRICT_BOTH
            return not self._sc_is_profit_threshold_entry(side, buys, sells, avg_spread)
        # RESTRICT_ONE_SIDE_BUY(1): SELL blocked
        if r == 1 and side == "sell":
            return not self._sc_is_profit_threshold_entry(side, buys, sells, avg_spread)
        # RESTRICT_ONE_SIDE_SELL(2): BUY blocked
        if r == 2 and side == "buy":
            return not self._sc_is_profit_threshold_entry(side, buys, sells, avg_spread)
        return False

    def _sc_apply_post_compensation_restriction(self, buy_cnt: int, sell_cnt: int) -> None:
        """補填決済後に restriction を設定する"""
        if buy_cnt < sell_cnt:
            self._sc_restriction = 1  # RESTRICT_ONE_SIDE_BUY
            self._log("[SC][RESTRICT] One-side BUY restriction activated", tag="SC", level=1)
        elif buy_cnt > sell_cnt:
            self._sc_restriction = 2  # RESTRICT_ONE_SIDE_SELL
            self._log("[SC][RESTRICT] One-side SELL restriction activated", tag="SC", level=1)
        else:
            self._sc_restriction = 0

    def _sc_check_restriction_transition(self, buy_cnt: int, sell_cnt: int) -> None:
        """One-side → Both / Both → One-side の遷移チェック"""
        r = self._sc_restriction
        if r in (1, 2) and buy_cnt == sell_cnt > 0:
            self._sc_restriction = 3  # RESTRICT_BOTH
            self._log("[SC][RESTRICT] Transition to both-side restriction", tag="SC", level=1)
        elif r == 3 and buy_cnt != sell_cnt:
            self._sc_restriction = 1 if buy_cnt < sell_cnt else 2
            self._log("[SC][RESTRICT] Transition to one-side restriction", tag="SC", level=1)

    # ------------------------------------------------------------------
    # §SC-1: 同数決済 (Equal Count Close)
    # ------------------------------------------------------------------
    def _sc_log_equal_skip(self, reason: str, buys: list, sells: list) -> None:
        """Throttled diagnostics for why Equal Close was skipped."""
        try:
            now = time.time()
            last = float(getattr(self, "_sc_last_equal_skip_log_ts", 0.0))
            if (now - last) < 5.0:
                return
            self._sc_last_equal_skip_log_ts = now
            self._log(
                f"[SC][EQUAL][SKIP] {reason} (b={len(buys)} s={len(sells)})",
                tag="SC_EQ",
                level=2,
            )
        except Exception:
            pass

    def _sc_check_equal_close(self, buys: list, sells: list) -> bool:
        """
        BUY/SELLが同数で、片方全体が利益 → 利益側を選択的決済
        優先度 2 (Offsetより前)
        """
        if not self.smart_equal_count_enable:
            self._sc_log_equal_skip("disabled", buys, sells)
            return False
        if not buys or not sells:
            self._sc_log_equal_skip("one-side-empty", buys, sells)
            return False
        if len(buys) != len(sells):
            self._sc_log_equal_skip("count-not-equal", buys, sells)
            return False

        buy_profs  = [self._sc_effective_profit(p) for p in buys]
        sell_profs = [self._sc_effective_profit(p) for p in sells]
        buy_all_profit  = all(x >= 0 for x in buy_profs)
        sell_all_profit = all(x >= 0 for x in sell_profs)

        if not buy_all_profit and not sell_all_profit:
            self._sc_log_equal_skip("neither-side-all-profitable", buys, sells)
            return False

        # trigger側の反対側 (close_side) の利益ポジを決済
        trigger_side = "buy" if buy_all_profit else "sell"
        close_pos    = sells if trigger_side == "buy" else buys
        close_profs  = sell_profs if trigger_side == "buy" else buy_profs

        profit_poss = [(p, c) for p, c in zip(close_pos, close_profs) if c > 0]
        if not profit_poss:
            try:
                close_side = "sell" if trigger_side == "buy" else "buy"
                close_raw = [
                    float(getattr(p, "profit", 0.0) or 0.0)
                    for p in close_pos
                ]
                close_eff = [float(x) for x in close_profs]
                self._sc_log_equal_skip(
                    "no-profitable-pos-on-close-side"
                    f" trigger={trigger_side.upper()} close={close_side.upper()}"
                    f" close_eff={close_eff} close_raw={close_raw}",
                    buys,
                    sells,
                )
            except Exception:
                self._sc_log_equal_skip("no-profitable-pos-on-close-side", buys, sells)
            return False

        total_pnl = 0.0
        closed = 0
        for pos, pnl in profit_poss:
            if self._sc_close_pos(pos):
                total_pnl += pnl
                closed += 1
                self._log(
                    f"[SC][EQUAL] Closed {trigger_side.upper()} side profit pos "
                    f"#{pos.ticket} pnl={pnl:.2f}",
                    tag="SC", level=1
                )
        if closed > 0:
            self._sc_distribute_profit(total_pnl)
            if self._sc_restriction == 3:  # RESTRICT_BOTH
                self._sc_restriction = 0
                self._log("[SC][RESTRICT] Released by Equal Close", tag="SC", level=1)
            return True
        return False

    # ------------------------------------------------------------------
    # §SC-2: オフセット決済 (Offset Close)
    # ------------------------------------------------------------------
    def _sc_log_offset_skip(self, reason: str, buys: list, sells: list) -> None:
        """Throttled diagnostics for why Offset Close was skipped."""
        try:
            now = time.time()
            last = float(getattr(self, "_sc_last_offset_skip_log_ts", 0.0))
            if (now - last) < 5.0:
                return
            self._sc_last_offset_skip_log_ts = now
            self._log(
                f"[SC][OFFSET][SKIP] {reason} (b={len(buys)} s={len(sells)} r={int(getattr(self, '_sc_restriction', 0))})",
                tag="SC_OFF",
                level=2,
            )
        except Exception:
            pass

    def _sc_check_offset_close(self, buys: list, sells: list) -> bool:
        """
        偏りがある場合: 多数派の最大含み損 + 少数派の最大含み益をペア決済
        """
        if not self.smart_offset_enable:
            self._sc_log_offset_skip("disabled", buys, sells)
            return False
        if len(buys) == len(sells):
            self._sc_log_offset_skip("no-imbalance", buys, sells)
            return False

        maj_pos = buys  if len(buys)  > len(sells) else sells
        min_pos = sells if len(buys)  > len(sells) else buys
        maj_side = "buy" if len(buys) > len(sells) else "sell"
        min_side = "sell" if maj_side == "buy" else "buy"

        # IsOffsetRestricted チェック
        r = self._sc_restriction
        if r == 1 and maj_side == "sell":  # BUY minority restricted
            self._sc_log_offset_skip("blocked-by-restriction(r=1, maj=sell)", buys, sells)
            return False
        if r == 2 and maj_side == "buy":   # SELL minority restricted
            self._sc_log_offset_skip("blocked-by-restriction(r=2, maj=buy)", buys, sells)
            return False

        maj_pnl = sum(self._sc_effective_profit(p) for p in maj_pos)
        if maj_pnl <= 0:
            self._sc_log_offset_skip(f"majority-pnl<=0({maj_pnl:.2f})", buys, sells)
            return False
        maj_profitable = sum(1 for p in maj_pos if self._sc_effective_profit(p) >= 0)
        if maj_profitable <= len(maj_pos) / 2.0:
            self._sc_log_offset_skip(
                f"majority-profitable-not-over-half({maj_profitable}/{len(maj_pos)})",
                buys,
                sells,
            )
            return False

        # 多数派: 最大含み損ポジ
        worst_maj = min(maj_pos, key=lambda p: self._sc_effective_profit(p), default=None)
        if worst_maj is None or self._sc_effective_profit(worst_maj) >= 0:
            self._sc_log_offset_skip("no-losing-majority-pos", buys, sells)
            return False

        # 少数派: 最大含み益ポジ
        best_min = max(min_pos, key=lambda p: self._sc_effective_profit(p), default=None)
        if best_min is None or self._sc_effective_profit(best_min) <= 0:
            self._sc_log_offset_skip("no-winning-minority-pos", buys, sells)
            return False

        worst_loss  = self._sc_effective_profit(worst_maj)
        best_profit = self._sc_effective_profit(best_min)

        if best_profit <= abs(worst_loss):
            self._sc_log_offset_skip(
                f"pair-net-not-positive(minWin={best_profit:.2f},majLoss={worst_loss:.2f})",
                buys,
                sells,
            )
            return False

        pair_pnl = best_profit + worst_loss
        self._log(
            f"[SC][OFFSET] Pairing: {maj_side.upper()}(loss={worst_loss:.2f}) + "
            f"{min_side.upper()}(profit={best_profit:.2f}) = {pair_pnl:.2f}",
            tag="SC", level=1
        )
        ok1 = self._sc_close_pos(worst_maj)
        ok2 = self._sc_close_pos(best_min)
        if ok1 and ok2:
            self._sc_distribute_profit(pair_pnl)
            self._sc_reset_negative_pool_after_compensation("offset-full-close")
            return True
        if ok1 or ok2:
            partial = worst_loss if ok1 else best_profit
            self._log(f"[SC][OFFSET] Partial close! pnl={partial:.2f}", tag="SC", level=1)
            self._sc_distribute_profit(partial)
            self._sc_reset_negative_pool_after_compensation("offset-partial-close")
        return ok1 or ok2

    # ------------------------------------------------------------------
    # §SC-3: 補填決済 (Accumulated Profit Close)
    # ------------------------------------------------------------------
    def _sc_log_accum_skip(self, reason: str, buys: list, sells: list) -> None:
        """Throttled diagnostics for why Accumulated Close was skipped."""
        try:
            now = time.time()
            last = float(getattr(self, "_sc_last_accum_skip_log_ts", 0.0))
            if (now - last) < 5.0:
                return
            self._sc_last_accum_skip_log_ts = now
            self._log(
                f"[SC][ACCUM][SKIP] {reason} (b={len(buys)} s={len(sells)} pool={float(getattr(self, '_sc_pool', 0.0)):.2f})",
                tag="SC_ACC",
                level=2,
            )
        except Exception:
            pass

    def _sc_check_accumulated_close(self, buys: list, sells: list) -> bool:
        """
        Pool有効時: 多数派が全益・少数派に損がある場合、Pool残高を消費してペア決済
        """
        if not self.smart_pool_enable or self._sc_pool <= 0:
            self._sc_log_accum_skip("pool-disabled-or-empty", buys, sells)
            return False
        if len(buys) == len(sells):
            self._sc_log_accum_skip("no-imbalance", buys, sells)
            return False

        maj_pos  = buys  if len(buys) > len(sells) else sells
        min_pos  = sells if len(buys) > len(sells) else buys
        maj_side = "buy" if len(buys) > len(sells) else "sell"
        min_side = "sell" if maj_side == "buy" else "buy"

        # 多数派が全部利益であること
        if any(self._sc_effective_profit(p) < 0 for p in maj_pos):
            self._sc_log_accum_skip("majority-not-all-profitable", buys, sells)
            return False
        # 少数派に損ポジが存在すること
        if not any(self._sc_effective_profit(p) < 0 for p in min_pos):
            self._sc_log_accum_skip("no-losing-minority-pos", buys, sells)
            return False

        # 多数派: 最大利益ポジ
        best_maj = max(maj_pos, key=lambda p: self._sc_effective_profit(p), default=None)
        # 少数派: 最大損失ポジ
        worst_min = min(min_pos, key=lambda p: self._sc_effective_profit(p), default=None)
        if best_maj is None or worst_min is None:
            self._sc_log_accum_skip("pair-candidate-missing", buys, sells)
            return False

        best_pnl  = self._sc_effective_profit(best_maj)
        worst_pnl = self._sc_effective_profit(worst_min)
        pair_pnl  = best_pnl + worst_pnl

        # ペアがマイナスの場合、Pool残高で補える場合のみ実行
        if pair_pnl < 0 and abs(pair_pnl) > self._sc_pool:
            self._sc_log_accum_skip(
                f"pair-loss-exceeds-pool(pair={pair_pnl:.2f})",
                buys,
                sells,
            )
            return False

        self._log(
            f"[SC][ACCUM] Pairing: {maj_side.upper()}(profit={best_pnl:.2f}) + "
            f"{min_side.upper()}(loss={worst_pnl:.2f}) = {pair_pnl:.2f}, pool={self._sc_pool:.2f}",
            tag="SC", level=1
        )
        ok1 = self._sc_close_pos(best_maj)
        ok2 = self._sc_close_pos(worst_min)
        if ok1 and ok2:
            self._sc_distribute_profit(pair_pnl)
            self._sc_reset_negative_pool_after_compensation("accum-full-close")
            # ポジション再集計して restriction を設定
            fresh = self._sc_get_positions()
            bc = sum(1 for p in fresh if p.type == mt5.POSITION_TYPE_BUY)
            sc = len(fresh) - bc
            self._sc_apply_post_compensation_restriction(bc, sc)
            return True
        if ok1 or ok2:
            partial = best_pnl if ok1 else worst_pnl
            self._log(f"[SC][ACCUM] Partial close! pnl={partial:.2f}", tag="SC", level=1)
            self._sc_distribute_profit(partial)
            self._sc_reset_negative_pool_after_compensation("accum-partial-close")
            fresh = self._sc_get_positions()
            bc = sum(1 for p in fresh if p.type == mt5.POSITION_TYPE_BUY)
            sc = len(fresh) - bc
            self._sc_apply_post_compensation_restriction(bc, sc)
        return ok1 or ok2

    # ------------------------------------------------------------------
    # §SC-4: 目標決済 (Target Close)
    # ------------------------------------------------------------------
    def _sc_calculate_target(self, buys: list, sells: list, avg_spread: float) -> float:
        """スプレッドベースの目標利益 (USD) を計算"""
        if avg_spread <= 0:
            return 999999.0
        diff = abs(len(buys) - len(sells))
        if diff <= 0:
            return 999999.0
        info = mt5.symbol_info(self.symbol)
        if not info:
            return 999999.0
        pt = float(info.point)
        cs = float(info.trade_contract_size)
        maj_pos = buys if len(buys) > len(sells) else sells
        # 最小ロットから diff 本分を合計
        sorted_vols = sorted(p.volume for p in maj_pos)
        target = 0.0
        target_mult = float(self._sc_target_parameter_effective())
        for i, v in enumerate(sorted_vols):
            if i >= diff:
                break
            target += v * avg_spread * target_mult * pt * cs
        return target

    def _sc_check_target_close(
        self, buys: list, sells: list, avg_spread: float, total_pnl: float
    ) -> bool:
        """Target close trigger check (starts async two-phase close)."""
        if self._sc_close_active:
            return False
        if not self.smart_target_close_enable:
            return False

        if self.smart_target_mode.lower().startswith("spread"):
            if avg_spread <= 0 or len(buys) == len(sells):
                return False
            base_target = self._sc_calculate_target(buys, sells, avg_spread)
            scale = self._sc_target_scale()
            target = base_target * scale
        else:
            base_target = float(self.smart_target_parameter)
            scale = 1.0
            target = base_target

        if total_pnl < target:
            return False

        self._log(
            f"[SC][TARGET] Triggered! profit={total_pnl:.2f} >= target={target:.2f}",
            tag="SC", level=1
        )
        self._sc_start_target_close(buys, sells, manual=False)
        return True

    def _sc_has_any_target_position(self) -> bool:
        with _MT5_LOCK:
            poss = mt5.positions_get(symbol=self.symbol) or []
        for p in poss:
            try:
                if self._sc_is_target_magic(int(getattr(p, "magic", 0))):
                    return True
            except Exception:
                continue
        return False

    def _sc_start_target_close(self, buys: list, sells: list, manual: bool = False) -> None:
        self._sc_close_active = True
        self._sc_close_phase = SC_CLOSE_CLOSEBY
        self._sc_orig_buy_count = len(buys)
        self._sc_orig_sell_count = len(sells)
        self._sc_manual_close = bool(manual)
        self._sc_close_start_ts = time.time()
        self._sc_target_tickets = {int(getattr(p, "ticket", 0)) for p in (buys + sells)}
        self._sc_closeby_phase_start_ts = time.time()
        self._sc_closeby_pairs_requested = 0

        pairs = min(len(buys), len(sells))
        if not self._sc_can_use_close_by():
            pairs = 0
        if pairs <= 0:
            self._sc_close_phase = SC_CLOSE_GATE
            return

        buys_sorted = sorted(buys, key=lambda p: int(getattr(p, "ticket", 0)))
        sells_sorted = sorted(sells, key=lambda p: int(getattr(p, "ticket", 0)))
        buy_tickets = [int(getattr(p, "ticket", 0)) for p in buys_sorted]
        sell_tickets = [int(getattr(p, "ticket", 0)) for p in sells_sorted]
        self._log(f"[SC][TARGET] Phase1 close-by pairs={pairs}", tag="SC", level=1)
        for i in range(pairs):
            ok = self._sc_close_by_async(buy_tickets[i], sell_tickets[i])
            if ok:
                self._sc_closeby_pairs_requested += 1
                continue
            # close_by送信失敗時は成行にフォールバック
            self._log(
                f"[SC][TARGET] close_by send failed -> fallback market (BUY#{buy_tickets[i]} SELL#{sell_tickets[i]})",
                tag="SC",
                level=1,
            )
            try:
                self._sc_close_pos_async(buys_sorted[i])
            except Exception:
                pass
            try:
                self._sc_close_pos_async(sells_sorted[i])
            except Exception:
                pass

        if self._sc_closeby_pairs_requested <= 0:
            self._log("[SC][TARGET] Phase1 close-by unavailable/fail -> proceed with fallback flow", tag="SC", level=1)
            self._sc_close_phase = SC_CLOSE_GATE

    def _sc_trigger_manual_close(self) -> bool:
        poss = self._sc_get_positions()
        if not poss:
            return False
        buys = [p for p in poss if p.type == mt5.POSITION_TYPE_BUY]
        sells = [p for p in poss if p.type == mt5.POSITION_TYPE_SELL]
        self._sc_start_target_close(buys, sells, manual=True)
        return True

    def _sc_finish_target_close(self) -> None:
        realized = 0.0
        matched = 0
        seen_deals: set[int] = set()
        start_ts = float(getattr(self, "_sc_close_start_ts", 0.0) or 0.0)

        def _deal_ts(d) -> float:
            try:
                tms = int(getattr(d, "time_msc", 0) or 0)
                if tms > 0:
                    return float(tms) / 1000.0
            except Exception:
                pass
            try:
                return float(getattr(d, "time", 0.0) or 0.0)
            except Exception:
                return 0.0

        def _add_deal(d) -> None:
            nonlocal realized, matched
            try:
                d_id = int(getattr(d, "ticket", 0) or 0)
            except Exception:
                d_id = 0
            if d_id > 0:
                if d_id in seen_deals:
                    return
                seen_deals.add(d_id)
            matched += 1
            realized += (
                float(getattr(d, "profit", 0.0) or 0.0)
                + float(getattr(d, "swap", 0.0) or 0.0)
                + float(getattr(d, "commission", 0.0) or 0.0)
            )

        try:
            dt_from = datetime.datetime.fromtimestamp(max(0.0, self._sc_close_start_ts - 2.0))
            dt_to = datetime.datetime.now() + datetime.timedelta(seconds=60)
            deals = mt5.history_deals_get(date_from=dt_from, date_to=dt_to)
            out_entries = {
                int(getattr(mt5, "DEAL_ENTRY_OUT", 1)),
                int(getattr(mt5, "DEAL_ENTRY_OUT_BY", 3)),
            }
            if deals:
                for d in deals:
                    try:
                        if getattr(d, "symbol", "") != self.symbol:
                            continue
                        if not self._sc_is_target_magic(int(getattr(d, "magic", 0))):
                            continue
                        if int(getattr(d, "entry", -1)) not in out_entries:
                            continue
                        dts = _deal_ts(d)
                        if start_ts > 0 and dts > 0 and dts < (start_ts - 2.0):
                            continue
                        _add_deal(d)
                    except Exception:
                        continue
        except Exception:
            pass

        # Fallback: deal時間窓やmagicで取りこぼした場合、対象positionごとに再集計する
        if matched == 0 and self._sc_target_tickets:
            try:
                out_entries = {
                    int(getattr(mt5, "DEAL_ENTRY_OUT", 1)),
                    int(getattr(mt5, "DEAL_ENTRY_OUT_BY", 3)),
                }
                for t in sorted(self._sc_target_tickets):
                    try:
                        deals_pos = mt5.history_deals_get(position=int(t)) or []
                    except Exception:
                        deals_pos = []
                    for d in deals_pos:
                        try:
                            if getattr(d, "symbol", "") != self.symbol:
                                continue
                            if int(getattr(d, "entry", -1)) not in out_entries:
                                continue
                            dts = _deal_ts(d)
                            if start_ts > 0 and dts > 0 and dts < (start_ts - 2.0):
                                continue
                            _add_deal(d)
                        except Exception:
                            continue
            except Exception:
                pass

        if realized > 0.001:
            self._sc_distribute_profit(realized)
        elif realized < -0.001:
            self._log(f"[SC][TARGET] Realized pnl={realized:.2f} (negative), skip pool distribution", tag="SC", level=1)
        else:
            self._log(f"[SC][TARGET] Realized pnl={realized:.2f} (matched_deals={matched}), no distribution", tag="SC", level=1)

        self._sc_restriction = 0
        self._sc_close_active = False
        self._sc_close_phase = SC_CLOSE_NONE
        self._sc_orig_buy_count = 0
        self._sc_orig_sell_count = 0
        self._sc_manual_close = False
        self._sc_close_start_ts = 0.0
        self._sc_target_tickets = set()
        self._sc_closeby_phase_start_ts = 0.0
        self._sc_closeby_pairs_requested = 0
        self._log(f"[SC][TARGET] Close complete. realized={realized:.2f} matched={matched}", tag="SC", level=1)

    def _sc_manage_async_close(self) -> bool:
        if not self._sc_close_active:
            return False

        poss = self._sc_get_positions()
        buys = [p for p in poss if p.type == mt5.POSITION_TYPE_BUY]
        sells = [p for p in poss if p.type == mt5.POSITION_TYPE_SELL]

        if self._sc_close_phase == SC_CLOSE_CLOSEBY:
            expected_diff = abs(self._sc_orig_buy_count - self._sc_orig_sell_count)
            total_after = len(buys) + len(sells)
            pairs_done = (len(buys) == 0 or len(sells) == 0 or total_after <= expected_diff)
            if not pairs_done:
                elapsed = time.time() - float(getattr(self, "_sc_closeby_phase_start_ts", 0.0))
                if elapsed < float(SMART_CLOSEBY_WAIT_SEC):
                    return True
                self._log(
                    f"[SC][TARGET] Phase1 timeout {elapsed:.1f}s -> fallback market for remaining pairs",
                    tag="SC",
                    level=1,
                )
                # close_byが通らない場合、残存両建て分は成行で処理して先へ進む
                buys_sorted = sorted(buys, key=lambda p: int(getattr(p, "ticket", 0)))
                sells_sorted = sorted(sells, key=lambda p: int(getattr(p, "ticket", 0)))
                n_pair = min(len(buys_sorted), len(sells_sorted))
                for i in range(n_pair):
                    self._sc_close_pos_async(buys_sorted[i])
                    self._sc_close_pos_async(sells_sorted[i])
            self._sc_close_phase = SC_CLOSE_GATE

        if self._sc_close_phase == SC_CLOSE_GATE:
            poss = self._sc_get_positions()
            buys = [p for p in poss if p.type == mt5.POSITION_TYPE_BUY]
            sells = [p for p in poss if p.type == mt5.POSITION_TYPE_SELL]
            if not poss or not self._sc_has_any_target_position():
                self._sc_finish_target_close()
                return True

            avg_spread = self._sc_avg_spread()
            total_pnl = sum(self._sc_effective_profit(p) for p in poss)
            if self.smart_target_mode.lower().startswith("spread"):
                base_target = self._sc_calculate_target(buys, sells, avg_spread)
                scale = self._sc_target_scale()
                target = base_target * scale
                if not self._sc_manual_close and len(buys) == len(sells):
                    self._log("[SC][TARGET] Abort gate: equal counts after close_by", tag="SC", level=1)
                    self._sc_finish_target_close()
                    return True
            else:
                base_target = float(self.smart_target_parameter)
                scale = 1.0
                target = base_target

            if not self._sc_manual_close and total_pnl < target * 0.5:
                self._log(
                    f"[SC][TARGET] Abort gate FAILED: profit={total_pnl:.2f} < 50% target={target:.2f}",
                    tag="SC",
                    level=1,
                )
                self._sc_finish_target_close()
                return True

            self._sc_close_phase = SC_CLOSE_DIFF
            for p in poss:
                self._sc_close_pos_async(p)
            return True

        if self._sc_close_phase == SC_CLOSE_DIFF:
            if not self._sc_get_positions() or not self._sc_has_any_target_position():
                self._sc_finish_target_close()
            return True

        return True

    # ------------------------------------------------------------------
    # §SC-MAIN: メイン呼び出し (1tick 1発動まで)
    # ------------------------------------------------------------------
    def _check_smart_close(self) -> bool:
        """
        4段階の Smart Close を優先順位順に実行。
        いずれか1つが発動したら True を返し、残りはスキップ。
        """
        if self.trading_paused:
            return False
        try:
            self._sc_update_spread()
            self._sc_log_boot_once()
            if self._sc_close_active:
                self._sc_manage_async_close()
                self._sc_update_gui()
                return True

            poss = self._sc_get_positions()
            if not poss:
                # フラット時の状態リセット
                # (_check_total_profit_threshold_and_close が無効化されたためここで引き継ぐ)
                self._is_pyramid_mode = False
                if getattr(self, "_current_mode_str", "IDLE") != "IDLE":
                    self._current_mode_str = "IDLE"
                    if not self.headless and getattr(self, "_mon_vars", None):
                        try:
                            self._safe_set(self._mon_vars["mode_status"], "IDLE")
                        except Exception:
                            pass
                return False

            buys  = [p for p in poss if p.type == mt5.POSITION_TYPE_BUY]
            sells = [p for p in poss if p.type == mt5.POSITION_TYPE_SELL]
            buy_cnt  = len(buys)
            sell_cnt = len(sells)

            if buy_cnt == 0 and sell_cnt == 0:
                return False

            avg_spread = self._sc_avg_spread()
            buy_pnl    = sum(self._sc_effective_profit(p) for p in buys)
            sell_pnl   = sum(self._sc_effective_profit(p) for p in sells)
            total_pnl  = buy_pnl + sell_pnl

            # Restriction 遷移チェック
            self._sc_check_restriction_transition(buy_cnt, sell_cnt)

            # GUI更新 (毎tick)
            self._sc_update_gui()

            # 優先度 1: 同数決済 (Equal Count Close)
            if self._sc_check_equal_close(buys, sells):
                self._sc_update_gui()
                return True

            # 優先度 2: オフセット決済 (Offset Close)
            if self._sc_check_offset_close(buys, sells):
                self._sc_update_gui()
                return True

            # 優先度 3: 補填決済 (Accumulated Profit Close)
            if self._sc_check_accumulated_close(buys, sells):
                self._sc_update_gui()
                return True

            # 優先度 4: 目標決済 (Target Close)
            if self._sc_check_target_close(buys, sells, avg_spread, total_pnl):
                self._sc_update_gui()
                return True

            # Diagnostics: prove SC pipeline is running even when nothing fires.
            try:
                now = time.time()
                last = float(getattr(self, "_sc_last_pipe_log_ts", 0.0))
                if (now - last) >= 5.0:
                    self._sc_last_pipe_log_ts = now
                    self._log(
                        f"[SC][PIPE] none-fired after Equal->Offset->Accum->Target "
                        f"(b={buy_cnt} s={sell_cnt} r={int(getattr(self, '_sc_restriction', 0))} pnl={total_pnl:.2f})",
                        tag="SC_PIPE",
                        level=2,
                    )
            except Exception:
                pass

        except Exception as e:
            self._log(f"[SC] _check_smart_close error: {e}", tag="SC", level=1)
        return False

    def _sc_update_gui(self) -> None:
        """Smart Close の状態を個別タブの GUI 変数に反映する"""
        if self.headless or not getattr(self, "_mon_vars", None):
            return
        try:
            state = self.get_smart_close_state()
            self._safe_set(self._mon_vars["sc_state"], state)
            self._safe_set(self._mon_vars["sc_pool"],  f"${self._sc_pool:.2f}")
            self._safe_set(self._mon_vars["sc_fixed"], f"${self._sc_fixed:.2f}")
            poss = self._sc_get_positions()
            buys = [p for p in poss if p.type == mt5.POSITION_TYPE_BUY]
            sells = [p for p in poss if p.type == mt5.POSITION_TYPE_SELL]
            avg_spread = self._sc_avg_spread()
            if self.smart_target_mode.lower().startswith("spread"):
                if avg_spread > 0 and len(buys) != len(sells):
                    base_target = self._sc_calculate_target(buys, sells, avg_spread)
                    scale = self._sc_target_scale()
                    target = base_target * scale
                    eff = self._sc_target_parameter_effective()
                    txt = f"${target:.2f} ({eff:.1f}x, scale:{scale:.0f})"
                else:
                    txt = "n/a (need diff)"
            else:
                txt = f"${float(self.smart_target_parameter):.2f} (fixed)"
            self._safe_set(self._mon_vars["sc_target"], txt)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # §SC-STATE: 状態評価 + エントリーフィルター
    # ------------------------------------------------------------------
    def get_smart_close_state(self) -> str:
        """
        現在のポジション状況・制限フラグから状態を4段階で返す。
          "Good Condition"        : 両方向エントリー可
          "BUY with Caution"      : BUYは条件付き許可、SELLは自由
          "SELL with Caution"     : SELLは条件付き許可、BUYは自由
          "Entry Not Recommended" : 両方向エントリー不可
        """
        try:
            poss = self._sc_get_positions()
            buys  = [p for p in poss if p.type == mt5.POSITION_TYPE_BUY]
            sells = [p for p in poss if p.type == mt5.POSITION_TYPE_SELL]
            avg_spread = self._sc_avg_spread()

            buy_ok  = (
                self._sc_can_add_position("buy",  buys, sells, avg_spread)
                and not self._sc_is_entry_restricted("buy",  buys, sells, avg_spread)
            )
            sell_ok = (
                self._sc_can_add_position("sell", buys, sells, avg_spread)
                and not self._sc_is_entry_restricted("sell", buys, sells, avg_spread)
            )

            # ポジションが片側のみ存在し全て含み損の場合は Caution へ
            if buys and not sells and all(self._sc_effective_profit(p) < 0 for p in buys):
                buy_ok = False
            if sells and not buys and all(self._sc_effective_profit(p) < 0 for p in sells):
                sell_ok = False

            if buy_ok and sell_ok:
                state = "Good Condition"
            elif buy_ok and not sell_ok:
                state = "SELL with Caution"
            elif not buy_ok and sell_ok:
                state = "BUY with Caution"
            else:
                state = "Entry Not Recommended"

            self._sc_last_state = state
            return state
        except Exception:
            return "Good Condition"

    def _sc_should_block_caution_entry(self, side: str, sc_state: str) -> tuple[bool, str]:
        """Return (blocked, reason) for caution-state entry filtering."""
        side_l = str(side).lower()
        if side_l not in ("buy", "sell"):
            return False, ""

        is_buy_caution = (sc_state == "BUY with Caution" and side_l == "buy")
        is_sell_caution = (sc_state == "SELL with Caution" and side_l == "sell")
        if not (is_buy_caution or is_sell_caution):
            return False, ""

        if self.smart_caution_mode == "strict":
            return True, f"Smart Close: {side_l.upper()} Caution blocked (strict)"

        # extended mode (backward-compatible behavior)
        poss = self._sc_get_positions()
        ptype = mt5.POSITION_TYPE_BUY if side_l == "buy" else mt5.POSITION_TYPE_SELL
        loss_sum = sum(
            self._sc_effective_profit(p)
            for p in poss
            if p.type == ptype and self._sc_effective_profit(p) < 0
        )
        cnt = sum(1 for p in poss if p.type == ptype)
        loss_ok = loss_sum > -float(MAJORITY_LOSS_MID_USD)
        cnt_ok = cnt <= 2
        if loss_ok and cnt_ok:
            return False, ""
        return True, f"Smart Close: {side_l.upper()} Caution blocked (loss={loss_sum:.2f}, cnt={cnt})"

    def _sc_entry_block_reason(self, side: str) -> str:
        """Common Smart Close entry filter for all entry paths."""
        try:
            sc_state = self.get_smart_close_state()
            if sc_state == "Entry Not Recommended":
                return f"Smart Close: Entry Not Recommended ({str(side).upper()} blocked)"
            blocked, reason = self._sc_should_block_caution_entry(side, sc_state)
            if blocked:
                return reason
        except Exception:
            return ""
        return ""


    def _pairnet_try_close(self) -> int:
        if self.trading_paused: return 0  # 一時停止中
        if not self.pairnet_enable: return 0
        now = time.time()
        if now - self._last_pairnet_ts < self.pairnet_cooldown: return 0

        # [FIX] Isolation: Filter positions by timeframe group
        all_positions = self._get_my_positions()
        
        # Helper to classify positions
        def _get_group(pos):
            c = str(pos.comment).lower()
            if "pivot_strict" in c or "pivot_m15" in c or "swing" in c or "[m15]" in c: return "M15"
            if "pivot_m5both" in c or "pivot_m5" in c or "day" in c or "[m5]" in c: return "M5"
            if "pivot_h1assist" in c or "h1" in c or "[h1]" in c: return "H1"
            return "M1" # Default to Scalp

        # Group positions
        groups = {"M1": [], "M5": [], "M15": [], "H1": []}
        for p in all_positions:
            g = _get_group(p)
            groups[g].append(p)

        # Run Pairnet Logic per group (Currently only enabling M1 to protect Swing/Day)
        # We can enable M5/M15 pairnet in the future if desired, but for now Isolation is the priority.
        # User requested isolation ("Separate them").
        
        target_groups = ["M1"] # Only run pairnet for M1
        # If user wants M5 pairnet (M5 losers offset by M5 winners), add "M5" here.
        # For now, start with M1 to solve the immediate issue of Swing positions being eaten.
        
        total_closed = 0
        
        for g_name in target_groups:
            positions = groups[g_name]
            if len(positions) < 2:
                continue

            # --- Below is the existing logic, applied to 'positions' (subset) ---
            self._disarm_tp_by_reason("near"); self._disarm_tp_by_reason("stuck") # Reset flags for safety

            # 多数側＝担がれ側判定
            n_buy  = sum(1 for p in positions if p.type == mt5.POSITION_TYPE_BUY)
            n_sell = sum(1 for p in positions if p.type == mt5.POSITION_TYPE_SELL)
            if n_buy > n_sell:
                loser_side, winner_side = mt5.POSITION_TYPE_BUY,  mt5.POSITION_TYPE_SELL
            elif n_sell > n_buy:
                loser_side, winner_side = mt5.POSITION_TYPE_SELL, mt5.POSITION_TYPE_BUY
            else:
                # 引き分け時は“合計損益が悪い側”を担がれ側に
                sum_buy = sum(p.profit for p in positions if p.type == mt5.POSITION_TYPE_BUY)
                sum_sell= sum(p.profit for p in positions if p.type == mt5.POSITION_TYPE_SELL)
                if sum_buy < sum_sell:
                    loser_side, winner_side = mt5.POSITION_TYPE_BUY,  mt5.POSITION_TYPE_SELL
                elif sum_sell < sum_buy:
                    loser_side, winner_side = mt5.POSITION_TYPE_SELL, mt5.POSITION_TYPE_BUY
                else:
                    # 完全同点なら、全体で最悪のポジの方向
                    worst_global = min(positions, key=lambda p: p.profit)
                    loser_side = worst_global.type
                    winner_side = mt5.POSITION_TYPE_SELL if loser_side == mt5.POSITION_TYPE_BUY else mt5.POSITION_TYPE_BUY

            # stuck-aware 事前アーミング（多数側のみ評価）
            tick = mt5.symbol_info_tick(self.symbol)
            info = mt5.symbol_info(self.symbol)
            self._stuck_arm_if_needed(positions, tick, info, loser_side, winner_side)

            # 多数側の中から最不利を選ぶ
            losers = [p for p in positions if p.type == loser_side]
            if not losers:
                self._disarm_tp_by_reason("near")
                continue # loop next group instead of return
            worst = min(losers, key=lambda p: p.profit)
            if worst.profit >= 0:
                self._disarm_tp_by_reason("near")
                continue # loop next group

            # 反対側の勝ちポジを上から集める
            opp = [p for p in positions if p.type == winner_side and p.profit > 0]
            opp.sort(key=lambda p: p.profit, reverse=True)
            
            # ★v10機能4: 利益温存 - 動的に保護本数を決定
            preserved_tickets = self._get_preserve_tickets(positions)
            opp = [p for p in opp if p.ticket not in preserved_tickets]

            winners, net, picked = [], worst.profit, 0
            for cand in opp:
                winners.append(cand); net += cand.profit; picked += 1
                if picked >= self.pairnet_max_pos - 1 or net >= self.pairnet_min_net_profit:
                    break

            # ▼▼▼ Escape Mode: トレンド逆行時の緊急相殺 ▼▼▼
            # 多数派がトレンドに逆らっている場合、利益目標を下げてでも相殺を優先する
            is_escape_mode = False
            default_min_profit = float(getattr(self, "pairnet_min_net_profit", 0.4))
            effective_min_profit = default_min_profit
            
            # ★v10機能2: 偏り時の決済慎重化
            # 少数側（winner_side）を決済に使う = 偏りが悪化する可能性
            # 少数側が本当に少数なら、閾値を上げて慎重に
            if MINORITY_CLOSE_CAUTION:
                minority_side = mt5.POSITION_TYPE_BUY if n_buy < n_sell else mt5.POSITION_TYPE_SELL
                if winner_side == minority_side and abs(n_buy - n_sell) >= 2:
                    effective_min_profit *= float(MINORITY_CLOSE_MULT)
                    # self._log(f"[CAUTION] Minority close -> threshold x{MINORITY_CLOSE_MULT}", level=2)
            
            # 1. 多数派の判定
            cnt_buy = sum(1 for p in positions if p.type == mt5.POSITION_TYPE_BUY)
            cnt_sell = sum(1 for p in positions if p.type == mt5.POSITION_TYPE_SELL)
            net_exposure = cnt_buy - cnt_sell
            
            # 2. トレンド確認 (M15 & H1)
            # _tf_dir returns (dir, rh, rl, ev)
            d15, *_ = self._tf_dir("M15")
            dH1, *_ = self._tf_dir("H1")
            
            # 3. 逆行判定
            # Buy過多 (+net) かつ トレンド下落 (d < 0)
            # Sell過多 (-net) かつ トレンド上昇 (d > 0)
            # ※ M15とH1が一致、または片方が強い逆行を示している場合に発動
            trend_is_down = (d15 == -1 or dH1 == -1)
            trend_is_up   = (d15 == 1 or dH1 == 1)
            
            if net_exposure > 2 and trend_is_down:
                 is_escape_mode = True
                 effective_min_profit = 0.0 # Breakevenで逃げる
            elif net_exposure < -2 and trend_is_up:
                 is_escape_mode = True
                 effective_min_profit = 0.0 # Breakevenで逃げる
                 
            if is_escape_mode:
                self._log(f"[ESCAPE] Mode Valid! Exposure={net_exposure:+d}, Trend(M15={d15}, H1={dH1}) -> MinProfit {default_min_profit} -> {effective_min_profit}", level=1)

            # 閾値判定
            arm_th = effective_min_profit * self.pairnet_arm_ratio
            disarm_th = effective_min_profit * self.pairnet_disarm_ratio

            if winners and net >= arm_th and net < effective_min_profit:
                self._arm_tp(winners, reason="near")
                self._set_status(f"Armed pair-net (net={net:.2f}/{effective_min_profit:.2f}) {'[ESC]' if is_escape_mode else ''}")
            elif net < disarm_th or not winners:
                self._disarm_tp_by_reason("near")

            if winners and net >= effective_min_profit:
                if not self._rate_ok("orders", self.max_orders_per_min):
                    self._set_status("Pair-net skipped (rate limit)"); continue # loop next
                to_close = [worst] + winners
                
                # Snapshot for Discord
                snap_profit = sum(p.profit for p in to_close)
                snap_pos_list = list(to_close)

                self._log(f"FIRE pair-net (loser={'BUY' if loser_side==0 else 'SELL'}), "
                          f"worst={worst.ticket}, winners={[p.ticket for p in winners]}, net={net:.2f} {'[ESC]' if is_escape_mode else ''}")
                n = self._close_positions(to_close, is_offset=True)
                if n > 0:
                    self.send_notify(
                        title="[USD] Pair-Net Settlement",
                        profit=snap_profit,
                        positions=snap_pos_list,
                        reason=f"PairNet (Net={net:.2f}) {'[ESC]' if is_escape_mode else ''}"
                    )
                    total_closed += n

                self._last_pairnet_ts = now
                self._disarm_all()
                self._set_status(f"Pair-net closed {n} positions (net={net:.2f})")
                
                # Check if we should stop processing other groups if we closed something?
                # Probably safer to process all valid groups but adhere to rate limits.
                # Since rate limit is checked inside, we can continue or break.
                # Let's break to avoid too many closes in one tick.
                return n # Return counts immediately as per original contract

        return total_closed

    # ── P&L utilities ────────────────────────────────────────
    def _positions_sorted(self):
        # [FIX] Use isolated positions (my magic only)
        poss = self._get_cached_positions()
        return sorted(poss, key=lambda x: x.profit, reverse=True)

    @staticmethod
    def _select_close_batch(targets: list, top_n: int, acc_realized: float, min_profit: float) -> list:
        """
        利益ポジ上位N本 + 最悪損失ポジをまとめてクローズするためのバッチを組む。
        予測実現益(acc_realized+バッチ損益)がフロアを割る場合は、余剰の利益ポジを追加して補強する。
        """
        n = max(1, int(top_n))
        winners = [p for p in targets if float(getattr(p, "profit", 0.0)) > 0.0]
        losers = [p for p in targets if float(getattr(p, "profit", 0.0)) <= 0.0]
        winners.sort(key=lambda p: float(getattr(p, "profit", 0.0)), reverse=True)
        losers.sort(key=lambda p: float(getattr(p, "profit", 0.0)))

        batch: list = []
        if winners:
            batch.extend(winners[:n])
        if losers:
            batch.append(losers[0])  # 最悪損失1本

        if batch and winners:
            selected_ids = {id(p) for p in batch}
            batch_pnl = sum(float(getattr(p, "profit", 0.0) or 0.0) for p in batch)
            projected = acc_realized + batch_pnl
            if projected < min_profit:
                for p in winners[n:]:
                    if id(p) in selected_ids:
                        continue
                    batch.append(p)
                    selected_ids.add(id(p))
                    batch_pnl += float(getattr(p, "profit", 0.0) or 0.0)
                    projected = acc_realized + batch_pnl
                    if projected >= min_profit:
                        break
        return batch

    def _close_list(self, plist):
        if not plist: return 0
        # 既存の安全なクローズ（close_by→失敗時は成行）を再利用
        return self._close_positions(plist)  # 【既存】_close_positions:contentReference[oaicite:3]{index=3}

    def _close_all_positions(self):
        # [FIX] Use isolated positions (my magic only)
        poss = self._get_my_positions()
        return self._close_list(poss)

    def _close_positions_with_profit(self, positions: list) -> tuple[int, float]:
        """リスト内のポジションを成行決済し、(成功数, 実現利益) を返す"""
        count = 0
        realized = 0.0
        for p in positions:
            profit = float(getattr(p, "profit", 0.0))
            if self._market_close(p, p.volume):
                count += 1
                realized += profit
            time.sleep(0.05)
        return count, realized

    def _close_all_positions_with_floor(self, min_profit: float, skip_preserve: bool = False) -> tuple[int, bool]:
        """
        全決済中に利益がフロアを下回ったら中断し、残ポジは維持する。
        skip_preserve=True の場合、利益温存対象のチケットを除外して決済する。
        戻り値: (クローズ本数, 完了したか)
        """
        # エントリーを抑止（インスタンス限定）
        self._closing_in_progress = True
        self._closing_reason = "total_close"

        # [FIX] 全決済開始時にGrid指値を削除（決済中の約定を防ぐ）
        self._clear_all_grid_orders(reason="before total close")

        closed = 0
        loop_cnt = 0
        start_ts = time.time()
        MAX_LOOP = 50
        TIMEOUT = 30.0
        state = getattr(self, "_global_close_state", {"active": False, "realized_profit": 0.0, "started_ts": 0.0})
        acc_realized = float(state.get("realized_profit", 0.0) or 0.0)
        state["active"] = True
        if state.get("started_ts", 0.0) == 0.0:
            state["started_ts"] = start_ts
        self._global_close_state = state
        self._save_global_close_state()

        # 温存対象の取得
        skip_tickets = set()
        if skip_preserve:
            # [FIX] Isolation
            all_poss = self._get_my_positions()
            skip_tickets = self._get_preserve_tickets(all_poss)

        try:
            while True:
                loop_cnt += 1
                elapsed = time.time() - start_ts
                if loop_cnt > MAX_LOOP or elapsed > TIMEOUT:
                    self._log(f"[CLOSE] Timeout/MaxLoop reached (n={loop_cnt}, t={elapsed:.1f}s). Aborting.", level=1)
                    state["realized_profit"] = acc_realized
                    self._global_close_state = state
                    self._save_global_close_state()
                    return closed, False

                # [FIX] Isolation
                poss = self._get_my_positions()
                if not poss:
                    state.update({"active": False, "realized_profit": 0.0, "started_ts": 0.0})
                    self._global_close_state = state
                    self._save_global_close_state()
                    return closed, True
                
                # 決済対象の絞り込み
                targets = [p for p in poss if p.ticket not in skip_tickets]
                if not targets:
                    state.update({"active": False, "realized_profit": 0.0, "started_ts": 0.0})
                    self._global_close_state = state
                    self._save_global_close_state()
                    return closed, True

                # Accumulate realized profit from this session to avoid premature abort
                # when winners are closed first.
                current_unrealized = sum(float(getattr(p, "profit", 0.0) or 0.0) for p in poss)
                total_net = current_unrealized + acc_realized
                
                if total_net < min_profit:
                    self._log(f"[CLOSE] abort: Net PnL {total_net:.2f} (Unr:{current_unrealized:.2f}+Rel:{acc_realized:.2f}) < floor {min_profit:.2f}", tag="CLOSE", level=1)
                    self._set_status(f"Close aborted (Net {total_net:.2f} < floor {min_profit:.2f})")
                    state["realized_profit"] = acc_realized
                    self._global_close_state = state
                    self._save_global_close_state()
                    return closed, False

                batch = self._select_close_batch(
                    targets,
                    int(getattr(self, "smart_close_top_winners", SMART_CLOSE_TOP_WINNERS)),
                    acc_realized,
                    float(min_profit),
                )
                if not batch:
                    state.update({"active": False, "realized_profit": acc_realized, "started_ts": 0.0})
                    self._global_close_state = state
                    self._save_global_close_state()
                    return closed, True

                c, realized_this_batch = self._close_positions_with_profit(batch)
                closed += c
                acc_realized += realized_this_batch
                state["realized_profit"] = acc_realized
                self._global_close_state = state
                self._save_global_close_state()
                
                if c == 0:
                    return closed, False
                
                time.sleep(0.2)
        finally:
            self._closing_in_progress = False
            self._closing_reason = ""

    def _check_total_profit_threshold_and_close(self):
        """合計利益が閾値以上なら全クローズ（相殺とは独立）"""
        # ★v10: 建値監視（Breakeven Watch）のチェック
        self._check_breakeven_watch()
        
        # [FIX] Isolation
        poss = self._get_cached_positions()
        if not poss:
            self._nanpin_lock = False # Reset on flat
            self._nanpin_hedge_done = False  # v10.6: ヘッジフラグリセット
            self._nanpin_hedge_vol = 0.0     # v10.6: ヘッジ量リセット
            self._nanpin_hedge_last_ts = 0.0 # v10.6: クールダウンリセット
            self._update_hedge_gui()
            self._is_pyramid_mode = False
            self._current_mode_str = "IDLE"
            self._last_pivot_dir_memory = 0  # Reset pivot direction on flat
            # 総利益クローズ状態をリセット
            self._global_close_state = {"active": False, "realized_profit": 0.0, "started_ts": 0.0}
            self._save_global_close_state()
            if not self.headless and getattr(self, "_mon_vars", None):
                try: self._safe_set(self._mon_vars["mode_status"], "IDLE")
                except: pass
            return 0
        is_hold_period = False
        is_pure_trend_hold = False
        
        # [FIX] Define hold_sec_logic from current profile (was missing, causing NameError at line 7645)
        hold_sec_logic = getattr(self.profile, "hold_sec", self.term_min_hold_sec)
        
        # ★v10 Fix: Exclude Preserved Positions from Total Profit Check
        # to prevent "preserved profit" from triggering premature global close.
        preserved_tickets = self._get_preserve_tickets(poss)
        non_preserved_poss = [p for p in poss if p.ticket not in preserved_tickets]
        
        total_profit_calc_poss = []
        skip_preserve_val = True
        
        # === Only-Preserve State Logic (Simplified for check) ===
        is_only_preserve = (len(poss) > 0 and len(non_preserved_poss) == 0)
        
        if is_only_preserve:
            # Only Preserve State
            # 1本ならホールド(calc対象外)、2本以上なら決済対象にする
            if len(poss) >= 2:
                total_profit_calc_poss = poss
                skip_preserve_val = False
            else:
                total_profit_calc_poss = [] # 1本なら無視
        else:
            total_profit_calc_poss = non_preserved_poss
            skip_preserve_val = True
            
        # === Pre-Calculate Mode (Pyramid vs Nanpin) ===
        try:
            # Aliases for consistent logic
            buy_vol = sum(p.volume for p in poss if p.type == mt5.POSITION_TYPE_BUY)
            sell_vol = sum(p.volume for p in poss if p.type == mt5.POSITION_TYPE_SELL)
            total_vol = buy_vol + sell_vol
            total_cost = sum(p.volume * p.price_open for p in poss)
            avg_price_calc = total_cost / total_vol if total_vol > 0 else 0.0
            
            is_one_sided_calc = (buy_vol > 0 and sell_vol == 0) or (sell_vol > 0 and buy_vol == 0)
            is_pure_trend_hold = False
            is_hold_period = False
            
            # --- Forced Nanpin Mode (Lock) ---
            # If we were previously hedged or in Recovery, stay there until flat.
            # Also lock if currently hedged.
            if not is_one_sided_calc and total_vol > 0:
                self._nanpin_lock = True
                # v10.6: ヘッジはM1確定時に _check_nanpin_hedge で判定

                # [NEW] Auto-switch to Scalp (M1) for NANPIN recovery
                # Day (M5) or Swing (M15) positions are kept, but new entries use M1 logic
                if self.profile.name in ["Day (M5)", "Swing (M15)"]:
                    target = "Scalp (M1)"
                    if target in self.profiles and self.profile.name != target:
                        self._nanpin_original_profile = self.profile.name  # Save for restoration
                        self.profile = self.profiles[target]
                        self._log(f"[NANPIN] Auto-switched from {self._nanpin_original_profile} to {target} for recovery", level=1)
                        # Re-apply params
                        if hasattr(self.profile, "cd_sec"): 
                            self.pivot_cooldown_sec = float(self.profile.cd_sec)
                        if hasattr(self.profile, "hold_sec"): 
                            self.term_min_hold_sec = float(self.profile.hold_sec)
                        # GUI TF表示も即時同期
                        self._update_gui_labels()
            
            # [FIX] Try Unlock if One-Sided and Profitable (Recovery -> Trend)
            if self._nanpin_lock and is_one_sided_calc:
                 t_prof = sum(p.profit for p in poss)
                 u_th = max(0.5, float(globals().get("TOTAL_PROFIT_THRESHOLD", 0.1)) + 0.1)
                 if t_prof > u_th:
                     self._nanpin_lock = False
                     self._nanpin_hedge_done = False  # v10.6: ヘッジフラグリセット
                     self._nanpin_hedge_vol = 0.0     # v10.6: ヘッジ量リセット
                     self._nanpin_hedge_last_ts = 0.0 # v10.6: クールダウンリセット
                     self._update_hedge_gui()

                     # [NEW] Restore original profile when exiting NANPIN
                     if hasattr(self, "_nanpin_original_profile") and self._nanpin_original_profile:
                         target = self._nanpin_original_profile
                         if target in self.profiles and self.profile.name != target:
                             self.profile = self.profiles[target]
                             self._log(f"[NANPIN] Restored profile to {target} (exiting recovery)", level=1)
                             # Re-apply params
                             if hasattr(self.profile, "cd_sec"): 
                                 self.pivot_cooldown_sec = float(self.profile.cd_sec)
                             if hasattr(self.profile, "hold_sec"): 
                                 self.term_min_hold_sec = float(self.profile.hold_sec)
                             # GUI TF表示も即時同期
                             self._update_gui_labels()
                         self._nanpin_original_profile = None

            if is_one_sided_calc and not self._nanpin_lock:
                # Sort by TICKET to find the first position reliably
                by_ticket = sorted(poss, key=lambda p: p.ticket)
                # ★ Hold-guard: if single position is still within hold_sec, stay in PYRAMID (no NANPIN lock)
                if len(by_ticket) == 1:
                    try:
                        tick = mt5.symbol_info_tick(self.symbol)
                        if tick:
                            elapsed_sec = (tick.time_msc - by_ticket[0].time_msc) / 1000.0
                            if elapsed_sec <= hold_sec_logic:
                                is_pure_trend_hold = True
                                is_hold_period = True
                    except Exception:
                        pass

                if len(by_ticket) > 0:
                    first_open = by_ticket[0].price_open
                    if len(by_ticket) == 1:
                    # Single Pos: Pyramid (Trend) only if profitable.
                    # If losing, treat as Nanpin (Escape) to avoid immediate Breakeven Cut.
                        if by_ticket[0].profit > 0:
                            is_pure_trend_hold = True 
                    else:
                        base_th = float(globals().get("TOTAL_PROFIT_THRESHOLD", 0.1))
                        buffer_val = max(0.5, base_th + 0.1)
                        total_profit = sum(p.profit for p in poss)
                        
                        # Multi-Pos: Require Trend Structure AND Sufficient Profit
                        if buy_vol > 0:
                            if avg_price_calc >= first_open:
                                is_pure_trend_hold = (total_profit > buffer_val)
                        else:
                            if avg_price_calc <= first_open:
                                is_pure_trend_hold = (total_profit > buffer_val)
            
            # If logic determined we are NOT in Trend Hold, lock Nanpin.
            # ただしホールド期間中はロックしない（Scalpに切替わり55s化するのを防ぐ）
            if (not is_pure_trend_hold) and (not is_hold_period) and total_vol > 0:
                 self._nanpin_lock = True
                 # v10.6: ヘッジはM1確定時に _check_nanpin_hedge で判定

                 # [NEW] Auto-switch to Scalp (M1) for NANPIN recovery (second trigger point)
                 if self.profile.name in ["Day (M5)", "Swing (M15)"]:
                     target = "Scalp (M1)"
                     if target in self.profiles and self.profile.name != target:
                         if not hasattr(self, "_nanpin_original_profile") or not self._nanpin_original_profile:
                             self._nanpin_original_profile = self.profile.name  # Save for restoration
                             self.profile = self.profiles[target]
                             self._log(f"[NANPIN] Auto-switched from {self._nanpin_original_profile} to {target} for recovery", level=1)
                             # Re-apply params
                             if hasattr(self.profile, "cd_sec"): 
                                 self.pivot_cooldown_sec = float(self.profile.cd_sec)
                             if hasattr(self.profile, "hold_sec"): 
                                 self.term_min_hold_sec = float(self.profile.hold_sec)
                             # GUI TF表示も即時同期
                             self._update_gui_labels()
        except Exception:
             is_pure_trend_hold = False # Fallback
        
        # ── [UPDATED] 55s Rule for First Position ──
        # 1ポジ目は直後のノイズ（建値割れやRLタッチ）で即狩りされるのを防ぐため、
        # 55秒間はトレンド判定待ち(Hold)とする。
        # 55秒経過後: 
        #   含み益(>0) → Trend Mode (Pyramid) 継続。建値SL(Trail)で粘る。
        #   含み損(<=0) → Recovery Mode (Nanpin) 移行。Scalp Targetで微益撤退。
        if len(poss) == 1:
            import time
            try:
                p = poss[0]
                tick = mt5.symbol_info_tick(self.symbol)
                if tick:
                    now_msc = tick.time_msc
                    open_msc = p.time_msc
                    elapsed_sec = (now_msc - open_msc) / 1000.0
                    
                    if elapsed_sec <= hold_sec_logic:
                        is_pure_trend_hold = True # Wait (Hold)
                        is_hold_period = True # Guard Trail
                    else:
                        # After hold_sec_logic: Check Profitability
                        # If Profit > Buffer (0.5 USD), we hold (Pyramid).
                        # If Profit <= Buffer, we treat as Nanpin/Weak to escape at small target.
                        # This ensures "dipping negative" or "stalling" allows exit at +0.1.
                        base_th = float(globals().get("TOTAL_PROFIT_THRESHOLD", 0.1))
                        buffer_val = max(0.5, base_th + 0.1)
                        
                        # [FIX] If profit > buffer, we allow unlocking to enter Pyramid mode.
                        if p.profit > buffer_val:
                            self._nanpin_lock = False

                        is_pure_trend_hold = (p.profit > buffer_val) and (not self._nanpin_lock)
                        if not is_pure_trend_hold:
                            self._nanpin_lock = True # Lock recovery
                            # v10.6: ヘッジはM1確定時に _check_nanpin_hedge で判定

            except Exception:
                pass 
        
        # Guard Clause: Prevent Trail Stop during Hold Period
        # If is_hold_period is True, we must NOT check for Trail Stop (Breakeven/RL).
        # We allow "Term Target" check (below) to proceed (in case of lucky spike > $50).


        # ★v10: モード状態をメンバ変数に保存（1M Pair-Net等のガードに使用）
        self._is_pyramid_mode = is_pure_trend_hold
        
        # Headlessでも参照できるように文字列も更新
        ms = "WAIT"
        if len(poss) > 0:
            if is_hold_period:
                # [FIX] Dynamic Hold Time display
                h_sec = int(getattr(self, "term_min_hold_sec", 55))
                ms = f"HOLD ({h_sec}s)"
            elif is_pure_trend_hold: ms = "PYRAMID (Trend)"
            else: ms = "NANPIN (Recovery)"
        else:
            ms = "IDLE"
        self._current_mode_str = ms

        if not self.headless and getattr(self, "_mon_vars", None):
            try:
                self._safe_set(self._mon_vars["mode_status"], ms)
            except:
                pass

        # ▼▼▼ Trend/Breakeven Trailing Logic (Re-implemented) ▼▼▼
        # Determine if we are in "Pyramiding" situation (Net Profitable / Favorable Trend)
        
        # ★v10 Fix: Exclude Preserved Positions from Average Price & Trail Logic
        # Preserved positions should not "subsidize" new positions to hit trail stops early.
        preserved_tickets = self._get_preserve_tickets(poss)
        non_preserved_poss = [p for p in poss if p.ticket not in preserved_tickets]
        
        # === Only-Preserve State Logic ===
        is_only_preserve = (len(poss) > 0 and len(non_preserved_poss) == 0)
        
        calc_poss = []
        skip_preserve_for_trail = True
        
        if is_only_preserve:
            # [CASE 1] Only Preserved Positions exist
            # Force Pyramid Mode (Profit taking phase)
            is_pure_trend_hold = True
            self._nanpin_lock = False  # ★ Force Unlock Logic
            
            # Update Mode String for UI
            self._current_mode_str = "PYRAMID (Preserve)"
            if not self.headless and getattr(self, "_mon_vars", None):
                try: self._safe_set(self._mon_vars["mode_status"], "PYRAMID (Preserve)")
                except: pass

            if len(poss) == 1:
                # 1本でもトレール有効（利益保護優先）
                calc_poss = poss
                skip_preserve_for_trail = False  # 決済許可
            else:
                # 2本以上: トレール有効 (平均建値で撤退/利確)
                calc_poss = poss # 全て対象
                skip_preserve_for_trail = False # 決済許可
            
            # ★v10.1: Pyramidモードでは全ポジにBE-SL適用（55sルール + 5pips条件）
            info = mt5.symbol_info(self.symbol)
            pt = info.point if info else 0.01
            required_dist = 50.0 * pt  # 5 pips ≈ 0.5 USD for 0.01 lot
            
            tick = mt5.symbol_info_tick(self.symbol)
            if tick and not is_hold_period:
                for p in poss:
                    if p.sl == 0:  # SL未設定のものだけ
                        cur = tick.bid if p.type == mt5.POSITION_TYPE_BUY else tick.ask
                        if abs(cur - p.price_open) >= required_dist:
                            self._apply_breakeven_sl([p.ticket], buffer_pips=1.0)
        else:
            # [CASE 2] Mixed or No Preserve
            calc_poss = non_preserved_poss
            skip_preserve_for_trail = True
        
        total_vol = sum(p.volume for p in calc_poss)
        total_cost = sum(p.volume * p.price_open for p in calc_poss)
        avg_price = total_cost / total_vol if total_vol > 0 else 0.0
        
        info = mt5.symbol_info(self.symbol)
        tick = mt5.symbol_info_tick(self.symbol)
        
        if tick and info:
            bid, ask = tick.bid, tick.ask
            # Check overall exposure (based on calc_poss)
            buy_vol = sum(p.volume for p in calc_poss if p.type == mt5.POSITION_TYPE_BUY)
            sell_vol = sum(p.volume for p in calc_poss if p.type == mt5.POSITION_TYPE_SELL)
            
            is_net_long = buy_vol > sell_vol
            is_net_short = sell_vol > buy_vol
            
            # Trail Check Target Count
            _target_count = len(calc_poss)
            
            # [MODIFIED] Restrict Trail Stop to Pure Direction Only (No Hedge/Nanpin)
            # Define Minimum Profit Buffer (e.g. 0.5 pips) to prevent negative slippage close
            # トレーリングストップ発動判定
            # tick.point が存在しないため symbol_info から取得
            pt = info.point if info else 0.001
            min_profit_pts = 2.0 * pt
            
            # ★v10.1: 平均価格割れ即決済（Pyramidモード全般で発動）
            # Hold期間中は発動しない
            if (is_only_preserve or is_pure_trend_hold) and len(calc_poss) >= 2 and not is_hold_period:
                # ネットポジション方向を判定
                avg_p = avg_price
                if is_net_long and sell_vol == 0:
                    cur_p = bid
                    if cur_p < avg_p:
                        # 平均価格割れ → 即決済
                        self._log(f"[AVG-BREAK] BUY avg break: {cur_p:.3f} < {avg_p:.3f}", level=1)
                        snap_profit = sum(p.profit for p in poss)
                        n, completed = self._close_all_positions_with_floor(-99999.0, skip_preserve=False)
                        if n > 0:
                            self.send_notify(
                                title="[USD] Avg Price Break (BUY)",
                                profit=snap_profit,
                                positions=list(poss),
                                reason=f"Price {cur_p:.3f} < Avg {avg_p:.3f}"
                            )
                            self._note_close_event(side="buy")
                        return n
                elif is_net_short and buy_vol == 0:
                    cur_p = ask
                    if cur_p > avg_p:
                        # 平均価格割れ → 即決済
                        self._log(f"[AVG-BREAK] SELL avg break: {cur_p:.3f} > {avg_p:.3f}", level=1)
                        snap_profit = sum(p.profit for p in poss)
                        n, completed = self._close_all_positions_with_floor(-99999.0, skip_preserve=False)
                        if n > 0:
                            self.send_notify(
                                title="[USD] Avg Price Break (SELL)",
                                profit=snap_profit,
                                positions=list(poss),
                                reason=f"Price {cur_p:.3f} > Avg {avg_p:.3f}"
                            )
                            self._note_close_event(side="sell")
                        return n
            
            # 温存対象外(or Only-Preserve Multi)のポジションが1本以上ある時のみトレール発動
            if is_net_long and sell_vol == 0 and _target_count >= 1:
                current_price = bid
                # Condition: Profit OR Pyramid Mode. BUT NOT if still in initial Hold Period.
                # [MODIFIED] Must NOT be in Nanpin Lock to allow Trailing Stop.
                
                # Buffer Check: Ensure current price is heavily profitable (Avg + Buffer) before trailing
                stop_floor = avg_price + min_profit_pts
                
                should_check_trail = (not is_hold_period) and (not self._nanpin_lock) and (
                    current_price > stop_floor # Only activate if we are above the safe floor
                )
                
                if should_check_trail: # In Profit zone (approx)
                    # [FIX] Dynamic Trailing TF (Unified Support)
                    # Default from comment tag → fallback to profile name
                    trail_tf = "M1"
                    try:
                        comment_blob = " ".join(str(p.comment).lower() for p in poss)
                        if "[day]" in comment_blob:
                            trail_tf = "M5"
                        elif "[swing]" in comment_blob or "[swg]" in comment_blob or "[m15]" in comment_blob:
                            trail_tf = "M15"
                        elif "[sca]" in comment_blob or "[m1]" in comment_blob:
                            trail_tf = "M1"
                        elif "pivot_m5both" in comment_blob:
                            trail_tf = "M5"
                        elif "pivot_strict" in comment_blob:
                            trail_tf = "M15"
                    except: pass
                    if trail_tf == "M1":
                        try:
                            pname = str(getattr(getattr(self, "profile", None), "name", getattr(self, "_initial_profile_name", ""))).lower()
                            if "day" in pname or "m5" in pname:
                                trail_tf = "M5"
                            elif "swing" in pname or "m15" in pname:
                                trail_tf = "M15"
                        except: pass
                    try:
                        # Use Boss Position's Origin comment to switch trailing sensitivity
                        if len(poss) > 0:
                            c_str = str(poss[0].comment).lower()
                            if "pivot_strict" in c_str:     trail_tf = "M15"
                            elif "pivot_m5both" in c_str:   trail_tf = "M5" # or C1
                            elif "pivot_h1assist" in c_str: trail_tf = "H1"
                    except: pass
                    
                    # Get Dynamic RL
                    _d1, _rh1, _rl1, _ev1, _ = self._tf_dir(trail_tf)
                    # Stop Price = max(Floor, RL) ... Trail the RL but keep floor
                    stop_price = stop_floor
                    if _rl1 is not None:
                        stop_price = max(stop_price, float(_rl1))
                    
                    if current_price < stop_price:
                        # TRAILING STOP HIT
                        self._log(f"[TRAIL] Close All BUYs. Price {current_price:.3f} < Stop {stop_price:.3f} (Avg {avg_price:.3f}, RL {_rl1}, Floor {stop_floor:.3f})", level=1)
                        # Snapshot before close
                        snap_profit = sum(p.profit for p in poss)
                        snap_pos = list(poss)
                        
                        # ★v10: 温存対象を取得し、それ以外を決済 (Only-Preserveの場合は全決済)
                        # skip_preserve_for_trail is defined at top of method
                        
                        preserve_tickets = []
                        if skip_preserve_for_trail and PROFIT_PRESERVE_ENABLE:
                            preserve_tickets = list(self._get_preserve_tickets(poss))
                        
                        n, completed = self._close_all_positions_with_floor(-99999.0, skip_preserve=skip_preserve_for_trail)
                        
                        # ★v10: 温存ポジションに建値SLを設定（Free Ride）
                        if preserve_tickets:
                            be_count = self._apply_breakeven_sl(preserve_tickets, buffer_pips=1.0)
                            self._log(f"[TRAIL] Applied BE-SL to {be_count} preserved positions.", level=1)
                        
                        if n > 0 and completed:
                             self.send_notify(
                                title="[USD] Trail Stop Hit (BUY)",
                                profit=snap_profit,
                                positions=snap_pos,
                                reason=f"Price {current_price}<{stop_price}, RL={_rl1}"
                             )
                        if completed:
                            self._note_close_event(side="buy")
                            self._set_status(f"Trail Stop (Buy): closed {n}")
                        return n

            elif is_net_short and buy_vol == 0 and _target_count >= 1:
                current_price = ask
                # [MODIFIED] Must NOT be in Nanpin Lock to allow Trailing Stop.
                # Buffer Check
                stop_floor = avg_price - min_profit_pts

                should_check_trail = (not is_hold_period) and (not self._nanpin_lock) and (
                     current_price < stop_floor # Only activate if we are below the safe floor
                )
                
                if should_check_trail: # In Profit zone (approx)
                    # [FIX] Dynamic Trailing TF (Unified Support)
                    trail_tf = "M1"
                    try:
                        comment_blob = " ".join(str(p.comment).lower() for p in poss)
                        if "[day]" in comment_blob:
                            trail_tf = "M5"
                        elif "[swing]" in comment_blob or "[swg]" in comment_blob or "[m15]" in comment_blob:
                            trail_tf = "M15"
                        elif "[sca]" in comment_blob or "[m1]" in comment_blob:
                            trail_tf = "M1"
                        elif "pivot_m5both" in comment_blob:
                            trail_tf = "M5"
                        elif "pivot_strict" in comment_blob:
                            trail_tf = "M15"
                    except: pass
                    if trail_tf == "M1":
                        try:
                            pname = str(getattr(getattr(self, "profile", None), "name", getattr(self, "_initial_profile_name", ""))).lower()
                            if "day" in pname or "m5" in pname:
                                trail_tf = "M5"
                            elif "swing" in pname or "m15" in pname:
                                trail_tf = "M15"
                        except: pass

                    # Get Dynamic RH
                    _d1, _rh1, _rl1, _ev1, _ = self._tf_dir(trail_tf)
                    # Stop Price = min(Floor, RH)
                    stop_price = stop_floor
                    if _rh1 is not None:
                        stop_price = min(stop_price, float(_rh1))
                
                    if current_price > stop_price:
                        # TRAILING STOP HIT
                        self._log(f"[TRAIL] Close All SELLs. Price {current_price:.3f} > Stop {stop_price:.3f} (Avg {avg_price:.3f}, RH {_rh1}, Floor {stop_floor:.3f})", level=1)
                        # Snapshot before close
                        snap_profit = sum(p.profit for p in poss)
                        snap_pos = list(poss)
                        
                        # ★v10: 温存対象を取得し、それ以外を決済 (Only-Preserveの場合は全決済)
                        # skip_preserve_for_trail is defined at top of method
                        
                        preserve_tickets = []
                        if skip_preserve_for_trail and PROFIT_PRESERVE_ENABLE:
                            preserve_tickets = list(self._get_preserve_tickets(poss))
                        
                        n, completed = self._close_all_positions_with_floor(-99999.0, skip_preserve=skip_preserve_for_trail)
                        
                        # ★v10: 温存ポジションに建値SLを設定（Free Ride）
                        if preserve_tickets:
                            be_count = self._apply_breakeven_sl(preserve_tickets, buffer_pips=1.0)
                            self._log(f"[TRAIL] Applied BE-SL to {be_count} preserved positions.", level=1)
                        
                        if n > 0 and completed:
                             self.send_notify(
                                title="[USD] Trail Stop Hit (SELL)",
                                profit=snap_profit,
                                positions=snap_pos,
                                reason=f"Price {current_price}>{stop_price}, RH={_rh1}"
                             )
                        if completed:
                            self._note_close_event(side="sell")
                            self._set_status(f"Trail Stop (Sell): closed {n}")
                        return n

        # === ★ 55s Rule REMOVED for Refactor (Pyramid Mode) ===
        # (Former logic removed here)

        # === Total Profit Check ===
        # Strategy Split:
        # 1. Trend Mode (Pyramid/Single): Hold for Trail Or Big Target.
        #    Condition: One-Sided AND (Single Pos OR AvgPrice is better/equal to First Price)
        # 2. Recovery Mode (Nanpin/Hedge): Escape at Scalp Target (0.1).
        #    Condition: Hedged OR (One-Sided AND AvgPrice is worse than First Price)
        
        # (is_pure_trend_hold is already calculated above)

        # (is_pure_trend_hold is already calculated above)

        # ★v10 Fix: Exclude Preserved Positions from Total Profit Check
        # to prevent "preserved profit" from triggering premature global close.
        preserved_tickets = self._get_preserve_tickets(poss)
        # calc_poss definition
        total_profit_calc_poss = [p for p in poss if p.ticket not in preserved_tickets]
        
        # If no active positions (only preserved), total effective profit is 0 (ignore preserve)
        # Or should we allow global close if ONLY preserved exists? 
        # No, preserved should stay until offset or manual close.
        if not total_profit_calc_poss:
            return 0

        saved_close_profit = float(self._global_close_state.get("realized_profit", 0.0)) if hasattr(self, "_global_close_state") else 0.0
        total = sum(p.profit for p in total_profit_calc_poss) + saved_close_profit
        
        # Determine Threshold based on Mode
        close_threshold = float(TOTAL_PROFIT_THRESHOLD)
        if is_pure_trend_hold:
            # In Trend Hold Mode, we bypass the small scalp threshold.
            term_target = float(getattr(self, "term_target_profit_usd", 50.0))
            if term_target <= 0: term_target = 50.0
            close_threshold = term_target
            
        if total >= close_threshold:
            self._log(f"[TERM TARGET] Total Profit {total:.2f} >= Threshold {close_threshold:.2f}", level=1)
            # Snapshot
            snap_profit = total
            snap_pos = list(poss)
            
            n, completed = self._close_all_positions_with_floor(
                float(getattr(self, "close_min_profit_floor", 0.0)),
                skip_preserve=skip_preserve_val  # [FIX] Dynamic preserve logic
            )
            if n > 0 and completed:
                 self.send_notify(
                    title="[USD] Term Target Reached 🎯",
                    profit=snap_profit,
                    positions=snap_pos,
                    reason=f"Profit {total:.2f} >= {close_threshold:.2f}"
                 )
            
            if completed:
                self._note_close_event()
                reason = "Term Target" if is_pure_trend_hold else "Scalp Target"
                self._set_status(f"Total P/L {total:.2f} \u2265 {close_threshold:.2f} ({reason}): closed {n} positions")

                # [FIX] Freeze Prevention: Reset Logic Lock
                self._nanpin_lock = False
                self._nanpin_hedge_done = False  # v10.6: ヘッジフラグリセット
                self._nanpin_hedge_vol = 0.0     # v10.6: ヘッジ量リセット
                self._nanpin_hedge_last_ts = 0.0 # v10.6: クールダウンリセット
                self._update_hedge_gui()
                self._log(f"Total take-profit fired ({reason}): total={total:.2f}", level=1)

                # [FIX] Clear grid orders and reset pivot direction after total take-profit
                self._clear_all_grid_orders(reason="total take-profit")
                self._last_pivot_dir_memory = 0  # Reset pivot direction
                self._current_mode_str = "IDLE"  # Reset mode to IDLE
                self._log("[GRID] Grid and pivot direction reset after total take-profit", level=1)
            return n
        return 0


    # ── Profit Recycling Logic ──────────────────────────────
    def _exec_profit_recycling(self) -> int:
        """
        利益還元相殺 (Profit Recycling):
        - 既存の「同方向相殺(Maintenance)」とは別枠。
        - 目的: 余剰利益でLoserを消し、ポジション削減とBudget回復(回転)を促す。
        - Phase 1 (Boss Raid): 利益でWorst Loserを倒せるなら即実行。
        - Phase 2 (Savings Mode): Budgetに余裕があるなら、小物は無視して利益温存（Boss Raidのために貯金）。
        - Phase 3 (Survival Mode): Budgetが枯渇しているなら、小物を消してBudgetを回復させる。
        """
        if not getattr(self, "ENTRY_BUDGET_ENABLE", False):
            return 0
            
        n = 0
        poss = self._positions_sorted() # profit descend (Best Winner -> Worst Loser)
        if len(poss) < 2:
            return 0

        # 多数派含み益ロック: ロック対象チケットをwinnersから除外して温存
        _maj_lock = self._get_majority_profit_lock_tickets(poss)
        winners = [p for p in poss if p.profit > 0 and p.ticket not in _maj_lock]
        losers  = [p for p in poss if p.profit <= 0]
        if not winners or not losers:
            return 0

        # 全Winner利益
        total_winner_profit = sum(w.profit for w in winners)

        # --- Phase 1: Boss Raid (Worst Loser Check) ---
        worst_loser = losers[-1] # poss is sorted by profit descend, so last is worst
        # 必要コスト確保
        # Note: worst_loser.profit is negative. net = total_winner_profit + worst_loser.profit
        if (total_winner_profit + worst_loser.profit) >= getattr(self, "RECYCLE_MIN_NET_PROFIT", 0.0):
            # 実行決定
            # 必要なだけのWinnerを選ぶ
            needed_profit = abs(worst_loser.profit) + getattr(self, "RECYCLE_MIN_NET_PROFIT", 0.0)
            use_winners = []
            current_profit = 0.0
            for w in winners:
                use_winners.append(w)
                current_profit += w.profit
                if current_profit >= needed_profit:
                    break
            
            target_list = [worst_loser] + use_winners
            net = sum(p.profit for p in target_list)
            self._log(f"[RECYCLE] BOSS RAID! worst={worst_loser.ticket}({worst_loser.profit}), net={net:.2f}", level=1)
            n = self._close_list(target_list)
            if n > 0:
                self._note_close_event(side=("buy" if worst_loser.type == mt5.POSITION_TYPE_BUY else "sell"))
                self._set_status(f"Recycled Boss: {n} pos (net={net:.2f})")
                return n

        # --- Phase 2: Budget Check (Savings Mode) ---
        # 今のBudget状況を確認する
        # Worst Loserのサイド（一番困っている側）のBudgetを見る。
        worst_side_str = "buy" if worst_loser.type == mt5.POSITION_TYPE_BUY else "sell"
        budget_ok, info = self._entry_budget_check(worst_side_str)
        # Note: _entry_budget_check logic: ok = entries_win < limit
        limit = info.get("limit", 0)
        entries_win = info.get("entries_win", 0)
        remaining = limit - entries_win
        
        # 閾値確認（デフォルト3→1へ厳格化）
        threshold = int(getattr(self, "RECYCLE_BUDGET_THRESHOLD", 1))
        
        if remaining > threshold:
            # 余裕あり → 貯金モード
            # ただし Skewed Cleanup (偏り時の強制デトックス) かどうか確認
            # 条件: 片側10本以上 かつ 3倍以上の偏り
            is_skewed_emergency = False
            try:
                b_n = sum(1 for p in poss if p.type == mt5.POSITION_TYPE_BUY)
                s_n = sum(1 for p in poss if p.type == mt5.POSITION_TYPE_SELL)
                # worst_side_str is available
                w_n = b_n if worst_side_str == "buy" else s_n
                minor_n = s_n if worst_side_str == "buy" else b_n
                
                if w_n >= 10:
                     if minor_n == 0:
                         is_skewed_emergency = True
                     elif (w_n / float(minor_n)) >= 3.0:
                         is_skewed_emergency = True
            except:
                pass
            
            if not is_skewed_emergency:
                return 0
            else:
                self._log(f"[RECYCLE] Skewed Cleanup Triggered! {worst_side_str.upper()}={w_n} vs {minor_n}", level=1)

        # --- Proximity Check (あとちょっとでBoss倒せるなら我慢) ---
        # Bossカバー率
        coverage_ratio = total_winner_profit / abs(worst_loser.profit) if worst_loser.profit != 0 else 0
        prox_ratio = float(getattr(self, "RECYCLE_PROXIMITY_RATIO", 0.8))
        
        if coverage_ratio >= prox_ratio:
            # まだBudget苦しくても、あと少しで倒せるなら耐える
            self._log(f"[RECYCLE] Proximity Wait: coverage {coverage_ratio*100:.1f}% >= {prox_ratio*100:.0f}%. Holding fire.", level=2)
            return 0

        # --- Phase 3: Survival Rotation (Small Loser Cleanup) ---
        # Budgetが厳しいので、小物を消して枠を空ける
        
        executed_n = 0
        active_winners = list(winners) # copy
        
        # losers[0] is smallest loss (e.g. -100)
        for l in losers:
            if l.ticket == worst_loser.ticket:
                # WorstはPhase 1で無理だったのでスキップ
                continue
                
            needed = abs(l.profit) + getattr(self, "RECYCLE_MIN_NET_PROFIT", 0.0)
            
            current_use = []
            used_profit = 0.0
            
            # Try to satisfy from active_winners
            found = False
            for i, w in enumerate(active_winners):
                current_use.append(w)
                used_profit += w.profit
                if used_profit >= needed:
                    # Success
                    target = [l] + current_use
                    net = sum(p.profit for p in target)
                    self._log(f"[RECYCLE] Survival cleanup: loser={l.ticket}({l.profit}), net={net:.2f}", level=1)
                    
                    this_n = self._close_list(target)
                    executed_n += this_n
                    
                    if this_n > 0:
                        self._note_close_event(side=("buy" if l.type == mt5.POSITION_TYPE_BUY else "sell"))
                        self._set_status(f"Recycled Small: {this_n} pos")
                        return executed_n # Return immediately to avoid over-spending
                    
                    found = True
                    break
            
            if not found:
                break
                
        return executed_n

    def _check_pair_profit_and_close(self):
        n = 0
        poss_sorted = self._positions_sorted()
        if len(poss_sorted) < 2:
            # ★Fix: ポジション不足時の状態クリーンアップ
            # 1. ノーポジなら無条件リセット
            if len(poss_sorted) == 0:
                if self._offset_tx is not None or self._offset_state.get("realized_profit", 0.0) > 0 or self._offset_state.get("active", False):
                     self._log("[TX] Cleanup: No positions left. Resetting offset state.", level=1)
                     self._offset_tx = None
                     self._reset_offset_state()
                return 0
            
            # 2. 残り1ポジの場合: ActiveなBossが不在ならリセット
            if self._offset_tx:
                remaining = poss_sorted[0]
                if remaining.ticket != self._offset_tx.boss_ticket:
                     self._log(f"[TX] Cleanup: Boss {self._offset_tx.boss_ticket} gone (1 pos left). Resetting.", level=1)
                     self._offset_tx = None
                     self._reset_offset_state()
                else:
                     # Bossは残っている（相方待ち）
                     pass

            # ポジションが少なすぎる場合、トランザクションがあればキャンセル (Boss維持のケース以外)
            if self._offset_tx and len(poss_sorted) < 2: 
                 # Boss維持ならここには来ないはずだが念のため
                 if poss_sorted and poss_sorted[0].ticket == self._offset_tx.boss_ticket:
                     pass
                 else:
                     self._log(f"[TX] Cancelling transaction (insufficient positions)", level=1)
                     self._offset_tx = None
            return n


        # ★v10.1: ヘッジ状態チェック（BUY+SELL両方存在時のみ発動）
        has_buy = any(p.type == mt5.POSITION_TYPE_BUY for p in poss_sorted)
        has_sell = any(p.type == mt5.POSITION_TYPE_SELL for p in poss_sorted)
        
        # Saved利益を取得
        saved_profit = self._offset_state.get("realized_profit", 0.0)
        
        # 修正: Saved利益がある場合は片側のみでもクリーンアップを許可
        is_hedged = (has_buy and has_sell)
        can_cleanup = (len(poss_sorted) > 0 and saved_profit > 0.1)
        
        if not (is_hedged or can_cleanup):
            # ヘッジ状態でもなく、クリーンアップできる利益もない場合はスキップ
            return n

        # ★v10: 利益温存チェック（危機判定が効くため、捕まりリスクとのバランスが取れる）
        preserved_tickets = (
            self._get_preserve_tickets(poss_sorted)
            | self._get_majority_profit_lock_tickets(poss_sorted)
        )

        # 最大利益ポジを取得（温存対象なら代替を探す）
        most_profit = poss_sorted[0]
        if most_profit.ticket in preserved_tickets:
            found_alt = False
            for p in poss_sorted[1:]:
                if p.profit > 0 and p.ticket not in preserved_tickets:
                    most_profit = p
                    found_alt = True
                    break
            if not found_alt:
                # 温存対象（種玉）しか利益ポジがないので、相殺処理を中断して種玉を守る
                if self._offset_tx:
                    self._log(f"[TX] Cancelled: No non-preserved winners available.", level=1)
                    self._offset_tx = None
                return n
        
        # 現在の最悪ポジ (Boss候補)
        current_worst = poss_sorted[-1]

        # ═══════════════════════════════════════════════════════════════
        # ★ TRANSACTION STATE MANAGEMENT ★
        # ═══════════════════════════════════════════════════════════════
        
        # 既存トランザクションのチェック
        if self._offset_tx:
            tx = self._offset_tx
            
            # Boss存在確認
            boss_exists = any(p.ticket == tx.boss_ticket for p in poss_sorted)
            
            if not boss_exists:
                # Bossが消えた = 外部決済（個別SL等）でクローズされた
                self._log(f"[TX] Boss {tx.boss_ticket} closed externally. Cancelling transaction (no re-entry).", level=1)
                self._offset_tx = None
                self._reset_offset_state()  # ★v10.3 Fix: 状態・GUI・停滞タイマーをリセット
            elif current_worst.ticket != tx.boss_ticket:
                # Bossが入れ替わった = リセット
                # ★v10.3: 進行状況を保存（後でこのBossに戻った時に復元するため）
                if "boss_history" not in self._offset_state:
                     self._offset_state["boss_history"] = {}
                self._offset_state["boss_history"][tx.boss_ticket] = tx.winners_closed
                # ★v10.3 Fix: 保存のスロットリング（頻繁なBoss入れ替わりでI/O飽和を防ぐ）
                now_save = time.time()
                if now_save - getattr(self, "_last_offset_save_ts", 0.0) >= 1.0:
                    self._save_offset_state_to_disk()
                    self._last_offset_save_ts = now_save
                
                # ★v10.3: ログに利益も表示して安心させる
                cur_profit = self._offset_state.get("realized_profit", 0.0)
                self._log(f"[TX] Boss changed: {tx.boss_ticket} -> {current_worst.ticket}. Saved progress ({tx.winners_closed} wins, ${cur_profit:.2f}).", level=1)
                
                # ★v10.3 Fix: GUI更新（保留中の利益を表示）
                if not self.headless and getattr(self, "_mon_vars", None):
                    try:
                        wc = self._offset_state["winner_count_buy"] + self._offset_state["winner_count_sell"]
                        profit = self._offset_state.get("realized_profit", 0.0)
                        self._safe_set(self._mon_vars["offset_state"], f"Saved: W:{wc} ${profit:.1f}")
                    except Exception:
                        pass
                self._offset_tx = None
            else:
                # Bossは同じ = トランザクション続行（デバッグレベル3: 通常非表示）
                pass  # self._log(f"[TX] Resuming for Boss {tx.boss_ticket}. Winners: {tx.winners_closed}", level=3)

        # ═══════════════════════════════════════════════════════════════

        most_loss = current_worst

        # --- ① 異方向相殺（画像仕様に準拠） ---
        most_loss = current_worst

        # --- ① 異方向相殺（画像仕様に準拠） ---
        # 修正: Cleanup許可時は同方向でも入る
        if most_profit.type != most_loss.type or can_cleanup:
            
            # ★ トランザクション開始/再開判定
            if self._offset_tx is None:
                # 新規トランザクション開始
                # 新規トランザクション開始
                restored_count = 0
                if "boss_history" in self._offset_state:
                    restored_count = self._offset_state["boss_history"].get(most_loss.ticket, 0)
                
                self._offset_tx = OffsetTransaction(
                    boss_ticket=most_loss.ticket,
                    boss_side=int(most_loss.type),
                    winner_side=int(most_profit.type)
                )
                self._offset_tx.winners_closed = restored_count # 復元
                
                msg_extra = f"(Restored progress: {restored_count})" if restored_count > 0 else ""
                self._log(f"[TX] Started new transaction. Boss={most_loss.ticket}, Target Side={'BUY' if most_loss.type == mt5.POSITION_TYPE_BUY else 'SELL'} {msg_extra}", level=1)
            
            tx = self._offset_tx
            
            # 相殺候補を構築
            pick = [most_profit, most_loss]
            # ★Fix: Saved利益を加算してNet判定
            net = most_profit.profit + most_loss.profit + saved_profit
            winners_in_this_batch = 1  # most_profit
            
            # ★Scan開始ログ (Level 2) - 5秒スロットリング
            _now = time.time()
            if _now - getattr(self, "_last_tx_check_ts", 0) >= 5.0:
                self._log(f"[TX] Check: BossPL={most_loss.profit:.1f}, BestWinnerPL={most_profit.profit:.1f}, Saved={saved_profit:.1f}, Net={net:.1f} (Thresh={self.pair_profit_threshold})", level=2)
                self._last_tx_check_ts = _now

            # スキャンログ収集用
            _scan_count = 0
            if net < self.pair_profit_threshold:
                for i in range(1, len(poss_sorted) - 1):
                    cand = poss_sorted[i]
                    if cand.ticket in (most_profit.ticket, most_loss.ticket):
                        continue
                    # Winner側のみ追加（Boss側は対象外）& 温存対象は除外
                    if cand.type == tx.winner_side and cand.profit > 0 and cand.ticket not in preserved_tickets:
                        pick.append(cand)
                        net += cand.profit
                        winners_in_this_batch += 1
                        _scan_count += 1

                    if net >= self.pair_profit_threshold:
                        break
            
            # 詳細デバッグログ (Level 2) - ループ外
            if _scan_count > 0:
                self._log(f"[TX] Scan added {_scan_count} candidates. Final net={net:.2f}, total_pick={len(pick)}", level=2)

            if net >= self.pair_profit_threshold:
                # クローズ実行
                n = self._close_positions(pick, is_offset=True)
                # ★Logging: Netの内訳を表示 (Virtual, Real, Saved)
                real_pl = net - saved_profit
                
                # GUI Status
                self._set_status(f"Pair-net closed {n} (Net={net:.2f} Real={real_pl:.2f})")
                
                self._log(f"[TX] Pair-net combo close: tickets={[p.ticket for p in pick]}, "
                          f"VirtualNet={net:.2f} (RealPL={real_pl:.2f}, SavedUsed={saved_profit:.1f}), "
                          f"batch_winners={winners_in_this_batch}", level=1)
                
                # Close Event Counting
                buy_profit = sum(p.profit for p in pick if int(getattr(p, "type", -1)) == int(mt5.POSITION_TYPE_BUY))
                sell_profit = sum(p.profit for p in pick if int(getattr(p, "type", -1)) == int(mt5.POSITION_TYPE_SELL))
                if buy_profit > sell_profit:
                    self._note_close_event(side="buy")
                elif sell_profit > buy_profit:
                    self._note_close_event(side="sell")
                else:
                    self._note_close_event(side="buy")
                    self._note_close_event(side="sell")

                # 残存チェック
                # 残存チェック & Boss生存チェックのための最新ポジション取得
                try:
                    before_tickets = {p.ticket for p in pick}
                    
                    # ★Fix: MT5エラーハンドリング強化
                    # [FIX] Isolation
                    poss_obj = self._get_my_positions()
                    if poss_obj is None:
                        self._log("[TX][WARN] Failed to get positions (mt5 error) after close. Skipping state update.", level=1)
                        return # 状態更新せずに抜ける（二重計上防止）
                        
                    after = {p.ticket for p in poss_obj}
                    
                    remains = list(before_tickets & after)
                    if remains:
                        self._log(f"[TX][WARN] close mismatch. remains={remains}", level=1)
                except Exception:
                    pass

                # ★ トランザクション状態更新
                boss_closed = tx.boss_ticket not in after
                
                # Winner側の決済数をカウント（Boss以外 かつ 実際に消えたもの）
                # before_tickets と after (mt5.positions_get) の差分で判定
                if 'after' in locals() and 'before_tickets' in locals():
                    actual_closed_tickets = before_tickets - after
                else:
                    # fallback (remains check fail時など)
                    # [FIX] Isolation
                    current_tickets = {p.ticket for p in self._get_my_positions()}
                    actual_closed_tickets = {p.ticket for p in pick} - current_tickets

                winners_actually_closed = sum(1 for p in pick 
                                              if p.ticket in actual_closed_tickets 
                                              and p.ticket != tx.boss_ticket 
                                              and p.type == tx.winner_side)
                tx.winners_closed += winners_actually_closed
                tx.last_activity = time.time()
                
                self._log(f"[TX] State: winners_closed_this_batch={winners_actually_closed}, total_winners_closed={tx.winners_closed}, boss_closed={boss_closed}", level=1)

                if boss_closed:
                    # ★★★ Boss決済完了 = トランザクション完了 ★★★
                    tx.complete()
                    
                    # ══════════════════════════════════════════════════════════
                    # 特殊ケース: Bossが消え、かつ「温存以外のポジ」がない場合
                    # ══════════════════════════════════════════════════════════
                    # [FIX] Isolation
                    remaining_poss = self._get_my_positions()
                    rem_buy = sum(1 for p in remaining_poss if p.type == mt5.POSITION_TYPE_BUY)
                    rem_sell = sum(1 for p in remaining_poss if p.type == mt5.POSITION_TYPE_SELL)
                    
                    winner_side_str = "buy" if tx.winner_side == mt5.POSITION_TYPE_BUY else "sell"
                    is_now_one_sided = (rem_sell == 0) if tx.winner_side == mt5.POSITION_TYPE_BUY else (rem_buy == 0)
                    
                    # 温存対象を再取得し、温存外のポジ数をチェック
                    _preserved_after = self._get_preserve_tickets(remaining_poss)
                    _non_preserved_after = [p for p in remaining_poss if p.ticket not in _preserved_after]
                    
                    # 温存ポジのみ残った = リエントリーせず、ピラミッド移行
                    if is_now_one_sided and len(_non_preserved_after) == 0:
                        self._log(f"[TX] Boss {tx.boss_ticket} cleared. Only preserved seed remains. Skipping re-entry -> Pyramid Mode.", level=1)
                        self._is_pyramid_mode = True
                        self._nanpin_lock = False
                        self._offset_tx = None
                        return n
                    # ══════════════════════════════════════════════════════════

                    total_winners = tx.winners_closed
                    # (Consolidated log moved further down)
                    
                    # Re-entry実行
                    if total_winners > 0:
                        ok_risk, reason_risk = self._risk_allows_new()
                        # [FIX] Isolation
                        remaining_poss = self._get_my_positions()
                        rem_buy = sum(1 for p in remaining_poss if p.type == mt5.POSITION_TYPE_BUY)
                        rem_sell = sum(1 for p in remaining_poss if p.type == mt5.POSITION_TYPE_SELL)
                        bypass_cap = (rem_buy > 0 and rem_sell > 0)
                        
                        # ★v10.1: 再エントリーモード制御
                        reentry_mode = str(OFFSET_REENTRY_MODE).upper()
                        reentry_n = 0
                        bypass_budget_for_mode = True  # デフォルト: バジェット無視
                        
                        # ★v10.3: 段階的モード切替（Boss保有時間 + 停滞時間）
                        if bool(OFFSET_DYNAMIC_MODE):
                            try:
                                total_pos = rem_buy + rem_sell
                                imbalance = abs(rem_buy - rem_sell)
                                
                                # Boss側のポジション取得
                                # [FIX] Isolation
                                all_poss = self._get_my_positions()
                                boss_side_type = mt5.POSITION_TYPE_SELL if winner_side_str == "buy" else mt5.POSITION_TYPE_BUY
                                boss_positions = [p for p in all_poss if p.type == boss_side_type]
                                
                                # ★Boss保有時間（最古のBossから計算）
                                # 注意: p.timeはMT5サーバー時間なので、現在時刻もサーバー時間を使う
                                boss_age_min = 0
                                if boss_positions:
                                    oldest_boss_time = min(p.time for p in boss_positions)
                                    tick = mt5.symbol_info_tick(self.symbol)
                                    current_time = tick.time if tick else int(time.time())
                                    boss_age_min = (current_time - oldest_boss_time) / 60
                                
                                # ★停滞時間（JSON永続化）
                                stagnation = 0
                                stag_file = os.path.join(os.path.dirname(__file__), f"offset_stag_{self.symbol}.json")
                                if hasattr(self, "_last_offset_complete_ts"):
                                    stagnation = (time.time() - self._last_offset_complete_ts) / 60
                                else:
                                    # JSONから読み込み
                                    try:
                                        if os.path.exists(stag_file):
                                            with open(stag_file, "r") as f:
                                                stag_data = json.load(f)
                                                self._last_offset_complete_ts = stag_data.get("last_offset_complete_ts", time.time())
                                                stagnation = (time.time() - self._last_offset_complete_ts) / 60
                                        else:
                                            self._last_offset_complete_ts = time.time()
                                    except:
                                        self._last_offset_complete_ts = time.time()
                                
                                # 詳細デバッグ (Level 2)
                                self._log(f"[TX] Calc Stats: imbal={imbalance}, total={total_pos}, age={boss_age_min:.0f}m, stag={stagnation:.0f}m", level=2)
                                
                                # 閾値
                                boss_age_thresh = float(OFFSET_BOSS_AGE_LEGACY)
                                stag_legacy = float(OFFSET_STAG_LEGACY)
                                imbal_balanced = int(OFFSET_IMBAL_BALANCED)
                                imbal_direct = int(OFFSET_IMBAL_DIRECT)
                                overextend = int(OFFSET_OVEREXTEND_THRESHOLD)
                                
                                # ★v10.3: モード決定 (優先度変更: 偏りはBALANCEDが最強)
                                # 1. 偏りがある → BALANCED (目標比率に合わせて一気に減らす)
                                # 2. 偏らないがポジ過多 → DIRECT (1本ずつ着実に減らす)
                                # 3. 古い/停滞 → LEGACY
                                
                                if imbalance >= imbal_balanced:
                                    reentry_mode = "BALANCED"
                                    reason = f"均衡回復 (imbal={imbalance})"
                                elif total_pos >= overextend:
                                    reentry_mode = "DIRECT"
                                    reason = f"ポジ過多縮小 (pos={total_pos})"
                                elif boss_age_min >= boss_age_thresh or stagnation >= stag_legacy:
                                    reentry_mode = "LEGACY"
                                    reason = f"早期相殺 (age={boss_age_min:.0f}m, stag={stagnation:.0f}m)"
                                else:
                                    reentry_mode = "LEGACY"
                                    reason = "デフォルト"
                                
                                # 動的モード変更時のみログ
                                _old_dyn = getattr(self, "_last_dyn_mode", "")
                                if reentry_mode != _old_dyn:
                                    self._log(f"[TX] Dynamic Mode Changed: {_old_dyn} -> {reentry_mode} ({reason})", level=2)
                                    self._last_dyn_mode = reentry_mode
                                
                            except Exception as e:
                                self._log(f"[TX] Dynamic mode error: {e}, fallback to {reentry_mode}", level=2)
                            
                            # GUI表示更新
                            if not self.headless and getattr(self, "_mon_vars", None):
                                try:
                                    self._safe_set(self._mon_vars["offset_mode"], f"Dyn:{reentry_mode}")
                                except: pass
                        
                        if reentry_mode == "NONE":
                            # 再エントリーなし
                            self._log(f"[TX] Re-entry skipped (MODE=NONE)", level=2)
                            reentry_n = 0
                        elif reentry_mode == "LEGACY":
                            # 従来通り: 閉じた勝ちポジ数 + Entry Budget加算
                            reentry_n = int(total_winners)
                            bypass_budget_for_mode = False  # バジェットに加算
                        elif reentry_mode == "BALANCED":
                            # バランス型: 相手側 + 動的バッファまで許可 + Entry Budget加算なし
                            total_pos = rem_buy + rem_sell
                            buffer_base = int(OFFSET_REENTRY_BUFFER_BASE)
                            buffer_divisor = max(1, int(OFFSET_REENTRY_BUFFER_DIVISOR))
                            buffer_max = int(OFFSET_REENTRY_BUFFER_MAX)
                            dynamic_buffer = min(buffer_base + (total_pos // buffer_divisor), buffer_max)
                            
                            if winner_side_str.lower() == "buy":
                                opposite_count = rem_sell
                                current_count = rem_buy
                            else:
                                opposite_count = rem_buy
                                current_count = rem_sell
                            
                            # ★重要: cap計算。既に決済が走った後の数(current_count)をベースにする
                            cap = max(0, (opposite_count + dynamic_buffer) - current_count)
                            reentry_n = min(int(total_winners), cap)
                            
                            bypass_budget_for_mode = False
                        elif reentry_mode == "DIRECT":
                            # DIRECT型: used_extra分の再発注 (total_winners - 1)
                            reentry_n = max(0, int(total_winners) - 1)
                            bypass_budget_for_mode = True 
                        else:
                            reentry_n = int(total_winners)
                            bypass_budget_for_mode = False
                        
                        # ★相殺完了サマリーログ (Level 1)
                        summary = f"[TX] Finalized: WinnersClosed={total_winners}, Mode={reentry_mode} -> Re-entry={reentry_n}"
                        if reentry_mode == "BALANCED":
                            summary += f" (Cap={cap}, Opp={opposite_count}, Cur={current_count})"
                        elif reentry_mode == "DIRECT":
                            summary += f" (Reduction: used_extra={reentry_n})"
                        self._log(summary, level=1)
                        
                        if reentry_n > 0 and ok_risk:
                            self._fire_offset_entries(
                                side=winner_side_str,
                                vol=float(self.lot),
                                n=reentry_n,
                                origin="offset_tx_complete",
                                bypass_budget=bypass_budget_for_mode,
                                is_hedged_offset=bypass_cap
                            )
                            self._note_entry_event(side=winner_side_str)
                        elif reentry_n > 0 and not ok_risk:
                            self._log(f"[TX] Re-entry deferred to queue: {reason_risk} (n={reentry_n})", level=2)
                            self._offset_retry_queue.append({
                                "side": winner_side_str,
                                "vol": float(self.lot),
                                "n": reentry_n,
                                "created": time.time(),
                                "is_hedged_offset": bypass_cap,
                                "bypass_budget": bypass_budget_for_mode
                            })
                    
                    # トランザクション終了
                    self._reset_offset_state()
                    self._offset_tx = None
                else:
                    # Boss未決済 = トランザクション継続（中断）
                    if n < len(pick):
                        tx.status = "paused"
                        self._log(f"[TX] Partial close (n={n}/{len(pick)}). Transaction paused. Waiting for next opportunity.", level=1)
                    # Boss以外が決済された場合は累積を記録済み、次回に続行

            # ①に入ったら、ここで戻る（②に落とさない）
            return n

        # --- ② 同方向相殺（画像仕様：クローズのみ／新規発注なし） ---
        
        # [MODIFIED] Pyramid Guard:
        # If we are in Pure Trend Hold mode (Pyramiding), DISABLE this offset logic.
        # We want to let winners run, not eat them to offset new spread loss.
        
        # Calculate stats for detection
        poss = poss_sorted # aliases
        buy_vol = sum(p.volume for p in poss if p.type == mt5.POSITION_TYPE_BUY)
        sell_vol = sum(p.volume for p in poss if p.type == mt5.POSITION_TYPE_SELL)
        is_one_sided = (buy_vol > 0 and sell_vol == 0) or (sell_vol > 0 and buy_vol == 0)
        
        if is_one_sided:
            # Check for Pyramid Mode (same logic as Total Profit check)
            # Sort by TICKET for stable First Position
            by_ticket = sorted(poss, key=lambda p: p.ticket)
            if len(by_ticket) > 0:
                first_open = by_ticket[0].price_open
                total_vol = buy_vol + sell_vol
                total_cost = sum(p.volume * p.price_open for p in poss)
                avg_price = total_cost / total_vol if total_vol > 0 else 0.0
                
                is_pyramid = False
                if len(by_ticket) == 1:
                    is_pyramid = True
                elif buy_vol > 0:
                     if avg_price >= first_open: is_pyramid = True
                else: 
                     if avg_price <= first_open: is_pyramid = True
                
                if is_pyramid:
                    # BLOCK Offset
                    return n

        total = sum(p.profit for p in poss_sorted)
        # 以前は loss <= TH (悪い時) だったが、メンテ目的に変更し total >= TH (良い時) にのみ実行
        if total >= OFFSET_ENABLED_ABOVE_PNL:
            worst = poss_sorted[-1]
            profit_sum = worst.profit
            positions_to_close = [worst]
            for p in poss_sorted:
                if p.ticket == worst.ticket:
                    continue
                if p.type != worst.type:
                    continue
                if p.profit <= 0:
                    continue
                profit_sum += p.profit
                positions_to_close.append(p)
                if profit_sum >= self.pair_profit_threshold:
                    n = self._close_list(positions_to_close)
                    
                    # Close Event Counting
                    sides_closed = set()
                    try:
                        for p in positions_to_close:
                            type_int = int(getattr(p, "type", -1))
                            if type_int == int(mt5.POSITION_TYPE_BUY):
                                sides_closed.add("buy")
                            elif type_int == int(mt5.POSITION_TYPE_SELL):
                                sides_closed.add("sell")
                        for s in sides_closed:
                            self._note_close_event(side=s)
                    except Exception:
                         self._note_close_event()

                    self._set_status(f"Same-direction offset close: {n} (net={profit_sum:.2f})")
                    self._log(f"Same-dir offset close: tickets={[p.ticket for p in positions_to_close]}, net={profit_sum:.2f}", level=1)
                    return n
        else:
            # ★特殊処理: 含み損がオーバーしている場合（total < OFFSET_ENABLED_ABOVE_PNL）
            # 同方向相殺は使えないので、異方向相殺を試みる
            # worst を選ぶ際、「反対側に利益ポジが存在する」損失ポジに限定する
            
            # 各方向の損失ポジと利益ポジを分類
            buy_losses  = [p for p in poss_sorted if p.type == mt5.POSITION_TYPE_BUY and p.profit < 0]
            sell_losses = [p for p in poss_sorted if p.type == mt5.POSITION_TYPE_SELL and p.profit < 0]
            buy_profits = [p for p in poss_sorted if p.type == mt5.POSITION_TYPE_BUY and p.profit > 0]
            sell_profits= [p for p in poss_sorted if p.type == mt5.POSITION_TYPE_SELL and p.profit > 0]
            
            # 相殺可能な候補を探す（損失ポジの反対側に利益ポジがあるペア）
            worst = None
            opp_profits = []
            
            # BUY損失 × SELL利益
            if buy_losses and sell_profits:
                worst = min(buy_losses, key=lambda p: p.profit)
                opp_profits = sell_profits
            # SELL損失 × BUY利益
            elif sell_losses and buy_profits:
                worst = min(sell_losses, key=lambda p: p.profit)
                opp_profits = buy_profits
            
            if worst and opp_profits:
                    # 利益が大きい順にソート
                    opp_profits.sort(key=lambda p: p.profit, reverse=True)
                    
                    profit_sum = worst.profit
                    positions_to_close = [worst]
                    
                    for p in opp_profits:
                        profit_sum += p.profit
                        positions_to_close.append(p)
                        if profit_sum >= self.pair_profit_threshold:
                            n = self._close_list(positions_to_close)
                            
                            # Close Event Counting
                            sides_closed = set()
                            try:
                                for p in positions_to_close:
                                    type_int = int(getattr(p, "type", -1))
                                    if type_int == int(mt5.POSITION_TYPE_BUY):
                                        sides_closed.add("buy")
                                    elif type_int == int(mt5.POSITION_TYPE_SELL):
                                        sides_closed.add("sell")
                                for s in sides_closed:
                                    self._note_close_event(side=s)
                            except Exception:
                                self._note_close_event()
                            
                            self._set_status(f"Cross-direction offset (emergency): {n} (net={profit_sum:.2f})")
                            self._log(f"Cross-dir offset (loss overflow): tickets={[p.ticket for p in positions_to_close]}, net={profit_sum:.2f}", level=1)

                            # --- v10: Emergency Re-entry ---
                            n_reentry = len(positions_to_close) - 1
                            if n_reentry > 0:
                                try:
                                    winner_side_type = int(opp_profits[0].type)
                                    side_str = "buy" if winner_side_type == mt5.POSITION_TYPE_BUY else "sell"
                                    
                                    ok_risk, reason_risk = self._risk_allows_new()
                                    if ok_risk:
                                        # 相殺後の残存ポジション判定（概算）
                                        # [FIX] Isolation
                                        remaining_poss = [p for p in self._get_my_positions() if p.ticket not in [x.ticket for x in positions_to_close]]
                                        rem_buy = sum(1 for p in remaining_poss if p.type == mt5.POSITION_TYPE_BUY)
                                        rem_sell = sum(1 for p in remaining_poss if p.type == mt5.POSITION_TYPE_SELL)
                                        bypass_cap = (rem_buy > 0 and rem_sell > 0)

                                        self._log(f"[ESCAPE-REFILL] Firing {n_reentry} {side_str}. Bypass Cap={bypass_cap}", level=1)
                                        self._fire_offset_entries(
                                            side=side_str, vol=float(self.lot), n=int(n_reentry),
                                            origin="escape_delta", bypass_budget=True, is_hedged_offset=bypass_cap
                                        )
                                        self._note_entry_event(side=side_str)
                                    else:
                                        self._offset_retry_queue.append({
                                            "side": side_str, "vol": float(self.lot), "n": int(n_reentry),
                                            "created": time.time(), "is_hedged_offset": False
                                        })
                                        self._log(f"[ESCAPE-REFILL] Deferred: {reason_risk}", level=1)
                                except Exception as e:
                                    self._log(f"[ESCAPE-REFILL] Error: {e}", level=2)

                            return n

        return n  # ← 最後も一貫して n を返す


    # ===== Minimal MAE (Momentum Assist Entries) =====
    def _mae_signal(self):
        # トグル & クールダウン
        if not getattr(self, 'mae_enable', False):
            return None
        if self._is_chop_blocked():
            return None
        now = time.time()
        last = getattr(self, '_last_mae_ts', 0.0)
        # self._log(f"MAE cooldown={self.mae_cooldown}s", tag="MAE", level=2)  # 一度だけ確認ログ

        cooldown = float(getattr(self, 'mae_cooldown', MAE_COOLDOWN_SEC))
        if time.time() - getattr(self, '_last_mae_ts', 0.0) < cooldown:
            return None

        # 必須情報
        info = mt5.symbol_info(self.symbol); tick = mt5.symbol_info_tick(self.symbol)
        if not info or not tick or not self.step_pts or self.mid is None:
            return None
        pt = info.point
        if pt <= 0:
            return None

        # 軽量ボラチェック（ATR5 / spread）
        spread_pts = int(round((tick.ask - tick.bid) / pt))
        min_atr_spread = float(getattr(self, 'mae_min_atr_spread', 1.2))
        try:
            rates = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_M5, 0, 12) or []
            if len(rates) >= 6 and spread_pts > 0:
                prev = rates[0]['close']; trs=[]
                for r in rates[1:]:
                    h,l,c = r['high'], r['low'], r['close']
                    tr = max(h-l, abs(h-prev), abs(l-prev)); trs.append(tr); prev = c
                atr = sum(trs[-5:]) / 5.0
                atr_pts = int(round(atr / pt))
                if atr_pts < min_atr_spread * spread_pts:
                    return None
        except Exception:
            pass  # ATR計算に失敗しても続行（頻度優先）

        # リセンタ基準(mid)から step*k のブレイクで順張り
        k = float(getattr(self, 'mae_break_k', 0.35))
        step = self.step_pts * pt
        mid_now = (tick.bid + tick.ask) / 2.0
        if mid_now >= self.mid + step * k:
            return "buy"
        if mid_now <= self.mid - step * k:
            return "sell"
        return None

    def _mae_fire(self, side: str):
        if self.trading_paused: return  # ★v10: 一時停止
        # MAE: エントリー直後にポジション情報がMT5に反映されるのを待たず、
        # 最小距離ガード（MAE）
        try:
            mid_now = float(self.mid) if self.mid is not None else 0.0
            if side == 'buy' and self._is_too_close_same_side('buy', mid_now,origin="mae"):
                self._log(_t("log.guard.buy", price=mid_now, min=self._min_entry_distance()), tag="TFLOW", level=1)
                return False
            if side == 'sell' and self._is_too_close_same_side('sell', mid_now,origin="mae"):
                self._log(_t("log.guard.sell", price=mid_now, min=self._min_entry_distance()), tag="TFLOW", level=1)
                return False
        except Exception:
            pass
        ok, why = self._risk_allows_new()
        if not ok:
            self._log(f"MAE blocked: {why}", tag="MAE", level=2); return False

        # 片側の同時本数制限（MAE）
        try:
            # [FIX] Isolation
            poss = self._get_my_positions()
            if side == "buy":
                live = [p for p in poss if p.type == mt5.POSITION_TYPE_BUY and (p.comment or "").startswith("mae-")]
            else:
                live = [p for p in poss if p.type == mt5.POSITION_TYPE_SELL and (p.comment or "").startswith("mae-")]
            if len(live) >= int(getattr(self, "mae_max_live", 1)):
                self._log(f"[MAE] live-exceed: {len(live)}/{int(getattr(self, 'mae_max_live', 1))}", tag="MAE", level=1)
                return False
        except Exception:
            pass


        info = mt5.symbol_info(self.symbol); tick = mt5.symbol_info_tick(self.symbol)
        if not info or not tick: return False

        # ★ volume は必ず正規化（10030対策）
        base = float(getattr(self, 'lot', 0.01))
        mult = float(getattr(self, 'mae_lot_mult', 0.35))
        vol  = self._norm_vol(base * mult)   # ← 既存ヘルパ :contentReference[oaicite:4]{index=4}

        price = tick.ask if side == "buy" else tick.bid

        self._send_entry_with_limit(
            side=side,
            vol=vol,
            origin="mae",
            price_hint=price,
            prefer_limit=True,
            comment=f"mae-{side}",
        )

        # “成功判定”はラッパーが値を返さないため、直後にポジを再取得して確認
        pnew = None
        # [FIX] Isolation
        poss = self._get_my_positions()
        for p in poss:
            if (p.comment or "").startswith("mae-") and p.type == (mt5.POSITION_TYPE_BUY if side=="buy" else mt5.POSITION_TYPE_SELL):
                pnew = p; break
        if not pnew:
            self._log("MAE: no new position detected after send", tag="MAE", level=2)
            return False

        # TP/SL 設定もラッパーで
        tp_price = self._tp_for_position(pnew)  # 既存のTP幅ロジック
        self._order_send_with_retry({
            "action":   mt5.TRADE_ACTION_SLTP,
            "symbol":   self.symbol,
            "position": pnew.ticket,
            "sl":       0.0,
            "tp":       tp_price,
            "deviation": DEVIATION,
            "magic":    self.magic,
            "comment":  "mae-set-tp",
            "origin":    "mae"   # ★追加（独立バケット）
        })

        self._last_mae_ts = time.time()
        self._log(f"MAE fired {side} vol={vol}", tag="TFLOW", level=1)
        return True
    # ───────────────────────────────────────────────────────────────────
    # M15レンジ確定＋一本目足色 判定専用関数
    def _m15_range_firstbar_bias(self, his, now, tf):
        """
        M15 足でレンジ確定したとき、その確定バーの陽線／陰線で
        RH／RL を一本目基準に設定し、目線（bias）を下／上へ設定する。
        his : 確定バーまでのバー配列（辞書形式）
        now : 形成中バー（辞書形式）
        tf  : 時間枠文字列（例 "M15"）
        戻り値: (mode, dir, line, ev)
          mode = "range_break_wait"｜"normal"
          dir  = +1｜-1｜0
          line = 水平ライン価格 (float) or None
          ev   = イベント文字列
        """
        bar = his[-1]
        open_  = float(bar["open"])
        close_ = float(bar["close"])
        high_  = float(bar["high"])
        low_   = float(bar["low"])

        if close_ < open_:
            # 陰線 → 高値突破まで “下目線”
            return ("range_break_wait", -1, high_, "レンジ確定‐陰線")
        elif close_ > open_:
            # 陽線 → 安値割るまで “上目線”
            return ("range_break_wait", +1, low_,  "レンジ確定‐陽線")
        else:
            # 始終同値 → 判定不能扱い
            return ("normal", 0, None, "レンジ確定-同値")

    # 方向判定メソッド（既存改修版）
    def _tf_dir(self, tf: str, bars: int = 300):
        tf_map = {
            "M1":  getattr(mt5, "TIMEFRAME_M1", 1),
            "M5":  getattr(mt5, "TIMEFRAME_M5", 5),
            "M15": getattr(mt5, "TIMEFRAME_M15", 15),
            "H1":  getattr(mt5, "TIMEFRAME_H1", 60),
            "H4":  getattr(mt5, "TIMEFRAME_H4", 240),
            "D1":  getattr(mt5, "TIMEFRAME_D1", 1440),
            "W1":  getattr(mt5, "TIMEFRAME_W1", 10080),   # [FIX] W1 Support
        }

        tf_const = tf_map.get(tf)
        if tf_const is None:
            return 0, None, None, "—"

        # リトライ付きレート取得 (Refresh aware) + Per-Bar Cache
        rates = None

        # Calculate Expected Current Bar Open Time for Freshness Check
        sec_map = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400, "W1": 604800}
        step_sec = sec_map.get(tf, 60)

        # Server time fallback logic - USE CACHED TICK
        tick_tmp = self._get_cached_tick() if hasattr(self, '_iter_tick') else mt5.symbol_info_tick(self.symbol)
        now_sec = time.time()
        if tick_tmp:
            # Update offset estimate
            self._server_time_offset = int(tick_tmp.time) - int(now_sec)

        # Use estimated server time
        offset = getattr(self, "_server_time_offset", 0)
        est_srv_time = int(now_sec) + offset

        expected_open = (est_srv_time // step_sec) * step_sec

        # ── Per-Bar Cache Check ──
        cached = self._tf_dir_cache.get(tf)
        if cached is not None:
            cached_bar_open, cached_result = cached
            if cached_bar_open >= expected_open:
                return cached_result

        # Reduce retries when we have cached fallback
        max_retries = 3 if cached is not None else 10

        for retry_count in range(max_retries):
            # [FIX] Thread-Safe Call - Acquire lock only for API call, not during sleep
            with _MT5_LOCK:
                rates = mt5.copy_rates_from_pos(self.symbol, tf_const, 0, bars)

            if rates is not None and len(rates) >= 6:
                # Freshness Check
                last_time = int(rates[-1]['time'])
                if last_time >= expected_open:
                    # Fresh data collected
                    break
            # Release lock before sleeping to avoid blocking other threads
            time.sleep(0.05 if retry_count < 2 else 0.1)

        # If fetch failed but we have cache, use cached result as fallback
        if (rates is None or len(rates) < 6) and cached is not None:
            return cached[1]

        if rates is None:
            self._log(f"[DIRDBG] {tf} rates=None (symbol={self.symbol})", tag="DIR", level=3)
            return 0, None, None, "—", 0
        if len(rates) < 6:
            self._log(f"[DIRDBG] {tf} rates.len={len(rates)} < 6 → skip", tag="DIR", level=3)
            return 0, None, None, "—", 0

        # Revert to Strict Slicing (Always Exclude Last Bar = Forming)
        his, now = rates[:-1], rates[-1]

        opens  = [r["open"]  for r in his]
        highs  = [r["high"]  for r in his]
        lows   = [r["low"]   for r in his]
        closes = [r["close"] for r in his]

        # ★v10: Body Break 判定 (RH/RL 実体抜け)
        body_break = 0
        try:
            if len(his) >= 2:
                his_pre = his[:-1]
                if len(his_pre) >= 2:
                    highs_pre  = [r["high"]  for r in his_pre]
                    lows_pre   = [r["low"]   for r in his_pre]
                    opens_pre  = [r["open"]  for r in his_pre]
                    closes_pre = [r["close"] for r in his_pre]
                    _, rh_prev, rl_prev, _ = _adjdir_from_series(
                        highs_pre, lows_pre, opens_pre, closes_pre, new_count=True
                    )
                    target_c = float(closes[-1])
                    if rh_prev is not None and target_c > float(rh_prev):
                        body_break = 1
                    elif rl_prev is not None and target_c < float(rl_prev):
                        body_break = -1
        except Exception:
            pass

        # 現行の（確定バーまででの）方向・RH/RL・イベント
        # こちらはUIや他ロジック用。最新足を含めて計算（引き締めあり）
        base_dir, rh, rl, ev = _adjdir_from_series(highs, lows, opens, closes, new_count=True)

        # Apply M1 Override Logic
        if tf == "M1" and base_dir == 0 and hasattr(self, "_m1_override"):
            ov = self._m1_override
            cur_price = float(now["close"])
            
            # Check if override is still valid
            valid = False
            if ov["dir"] > 0: # Driven UP
                if cur_price >= ov["low"]: # Still above support
                    base_dir, rl, rh = 1, ov["low"], None
                    ev += "(M1 Override: Up)"
                    valid = True
            elif ov["dir"] < 0: # Driven DOWN
                if cur_price <= ov["high"]: # Still below res
                    base_dir, rh, rl = -1, ov["high"], None
                    ev += "(M1 Override: Down)"
                    valid = True
            
            if not valid:
                 # Invalidate
                 del self._m1_override

        # ★ state入れ物の存在保証
        if not hasattr(self, "_pivot_state"):
            self._pivot_state = {}
        st = self._pivot_state.get(tf)

        is_range_prev = False  # 既定は False（M15でのみ上書き）

        # --- M15専用：レンジ確定した「そのバー」の足色で目線設定（陽線→RH／陰線→RL） ---
        # --- M15: Standard logic only (Special logic moved to M5) ---
        # --- M15専用: レンジ確定した「そのバー」の足色で目線設定（陽線→RH／陰線→RL） ---
        if tf == "M15":
            if len(his) >= 2:
                # Use FULL history (including last closed bar) to get final confirmed state
                opens_p  = [r["open"]  for r in his]
                highs_p  = [r["high"]  for r in his]
                lows_p   = [r["low"]   for r in his]
                closes_p = [r["close"] for r in his]
                _bd_p, rh_prev, rl_prev, _ev_prev = _adjdir_from_series(
                    highs_p, lows_p, opens_p, closes_p, new_count=True
                )
                
                # [FIX] Lookback Logic: Check if Bar[-1] is inside state(Bar[0]...Bar[-2])
                # Calculate state BEFORE the last bar
                his_pre = his[:-1]
                _, rh_old, rl_old, _ = _adjdir_from_series(
                     [r["high"] for r in his_pre],
                     [r["low"] for r in his_pre],
                     [r["open"] for r in his_pre],
                     [r["close"] for r in his_pre],
                     new_count=True
                )
                
                prev_bar = his[-1]
                hi_prev = float(prev_bar["high"])
                lo_prev = float(prev_bar["low"])
                
                # Check Inside: Last Bar is Inside "Old State"
                # If rh_old/rl_old is None (not enough history), fallback to false
                is_inside_range = False
                if rh_old is not None and rl_old is not None:
                     # Allow float epsilon
                     is_inside_range = (hi_prev <= float(rh_old) + 0.00001) and (lo_prev >= float(rl_old) - 0.00001)

                if st is None: st = {}
                st["dir"] = int(_bd_p)
                st["rh"] = rh_prev
                st["rl"] = rl_prev
                st["ev"] = _ev_prev

                if is_inside_range:
                    st["mode"] = "range-inherit"
                    ev += " (Range Inherit)"
                    st["ev"] += " (Inherit)"
                    # [User Req] Range established => Tighten RH/RL to this bar
                    st["rh"] = hi_prev
                    st["rl"] = lo_prev
                    self._pivot_state[tf] = st
                else:
                    # Normal Break / Outside Bar Resolved
                    pass
                    # Break detected -> Sync
                    if st is None: st = {}
                    st["dir"] = int(base_dir)
                    
                    # [FIX] Both Break History: Check Parent (H1)
                    if st["dir"] == 0:
                        p_dir, *_ = self._tf_dir("H1")
                        if p_dir is not None and int(p_dir) != 0:
                            st["dir"] = p_dir
                            ev += " (Ref:H1)"

                    st["rh"] = rh
                    st["rl"] = rl
                    st["ev"] = ev
                    st["mode"] = "break-sync"
                    self._pivot_state[tf] = st
                    # Fall through...

        # --- H1専用：レンジ確定した「そのバー」の足色で目線設定（M15ロジックの継承） ---
        if tf == "H1" and len(his) >= 2:
            opens_p  = [r["open"]  for r in his[:-1]]
            highs_p  = [r["high"]  for r in his[:-1]]
            lows_p   = [r["low"]   for r in his[:-1]]
            closes_p = [r["close"] for r in his[:-1]]
            _bd_p, rh_prev, rl_prev, _ev_prev = _adjdir_from_series(
                highs_p, lows_p, opens_p, closes_p, new_count=True
            )
            # [FIX] Lookback Logic (H1)
            his_pre = his[:-1]
            
            _, rh_old, rl_old, _ = _adjdir_from_series(
                 [r["high"] for r in his_pre],
                 [r["low"] for r in his_pre],
                 [r["open"] for r in his_pre],
                 [r["close"] for r in his_pre],
                 new_count=True
            )
            
            prev_bar = his[-1]
            hi_prev = float(prev_bar["high"])
            lo_prev = float(prev_bar["low"])
            
            is_inside_range = False
            if rh_old is not None and rl_old is not None:
                 is_inside_range = (hi_prev <= float(rh_old) + 0.00001) and (lo_prev >= float(rl_old) - 0.00001)

            if is_inside_range:
                if st is None: st = {}
                # H1 Range Inherit => Tighten
                rh, rl = hi_prev, lo_prev
                base_dir = _bd_p 
                ev += " (Range Inherit)"
                
                st["mode"] = "range-inherit"
                st["dir"] = int(base_dir)
                st["rh"] = rh
                st["rl"] = rl
                st["ev"] = ev
                
                self._pivot_state[tf] = st
            else:
                # Break detected -> Sync
                if st is None: st = {}
                st["dir"] = int(base_dir)
                # [FIX] 両抜け(dir==0)時はH4を参照（M15→H1, M5→M15, M1→M5と同パターン）
                if st["dir"] == 0:
                    pm = getattr(self, "profile", None).parent_map if getattr(self, "profile", None) else {}
                    ptf = pm.get(tf) or {"H1": "H4"}.get(tf)
                    if ptf:
                        p_dir, *_ = self._tf_dir(ptf)
                        if p_dir is not None and int(p_dir) != 0:
                            st["dir"] = int(p_dir)
                            ev += f" (Ref:{ptf})"
                st["rh"] = rh
                st["rl"] = rl
                st["ev"] = ev
                st["mode"] = "break-sync"
                self._pivot_state[tf] = st
                # Fall through...

        # --- M5専用 (M15のロジックを移植)：通常ロジックのみ ---
        if tf == "M5" and len(his) >= 2:
             # Use FULL history (including last closed bar)
             opens_p  = [r["open"]  for r in his]
             highs_p  = [r["high"]  for r in his]
             lows_p   = [r["low"]   for r in his]
             closes_p = [r["close"] for r in his]
             _bd_p, rh_prev, rl_prev, _ = _adjdir_from_series(
                 highs_p, lows_p, opens_p, closes_p, new_count=True
             )
             # [FIX] Lookback Logic (M5)
             his_pre = his[:-1]
             _, rh_old, rl_old, _ = _adjdir_from_series(
                 [r["high"] for r in his_pre],
                 [r["low"] for r in his_pre],
                 [r["open"] for r in his_pre],
                 [r["close"] for r in his_pre],
                 new_count=True
             )
             prev_bar = his[-1]
             hi_prev = float(prev_bar["high"])
             lo_prev = float(prev_bar["low"])
             
             is_inside_range = False
             if rh_old is not None and rl_old is not None:
                 is_inside_range = (hi_prev <= float(rh_old) + 0.00001) and (lo_prev >= float(rl_old) - 0.00001)

             if is_inside_range:
                 # M5 Range Inherit Logic => Tighten
                 if st is None: st = {}
                 rh, rl = hi_prev, lo_prev
                 base_dir = _bd_p
                 ev += " (Range Inherit)"
                 
                 st["mode"] = "range-inherit"
                 st["dir"] = int(base_dir)
                 st["rh"] = rh
                 st["rl"] = rl
                 st["ev"] = ev
                 
                 self._pivot_state[tf] = st
             else:
                 # Break detected -> Sync
                 if st is None: st = {}
                 st["dir"] = int(base_dir)
                 
                 # [FIX] Both Break History: Check Parent (M15)
                 if st["dir"] == 0:
                     p_dir, *_ = self._tf_dir("M15")
                     if p_dir is not None and int(p_dir) != 0:
                         st["dir"] = p_dir
                         ev += " (Ref:M15)"

                 st["rh"] = rh
                 st["rl"] = rl
                 st["ev"] = ev
                 st["mode"] = "break-sync"
                 self._pivot_state[tf] = st
                 # Fall through...

        # --- M1：前バー未ブレイク＝レンジ ---
        if tf == "M1" and len(his) >= 2:
            # Use FULL history (including last closed bar)
            opens_p  = [r["open"]  for r in his]
            highs_p  = [r["high"]  for r in his]
            lows_p   = [r["low"]   for r in his]
            closes_p = [r["close"] for r in his]
            _bd_p, rh_prev, rl_prev, _ = _adjdir_from_series(
                highs_p, lows_p, opens_p, closes_p, new_count=True
            )
            # [FIX] Lookback Logic (M1)
            his_pre = his[:-1]
            _, rh_old, rl_old, _ = _adjdir_from_series(
                 [r["high"] for r in his_pre],
                 [r["low"] for r in his_pre],
                 [r["open"] for r in his_pre],
                 [r["close"] for r in his_pre],
                 new_count=True
            )
            prev_bar = his[-1]
            hi_prev = float(prev_bar["high"])
            lo_prev = float(prev_bar["low"])

            is_inside_range = False
            if rh_old is not None and rl_old is not None:
                 is_inside_range = (hi_prev <= float(rh_old) + 0.00001) and (lo_prev >= float(rl_old) - 0.00001)

            if is_inside_range:
                # 改修: M1も同様
                if st is None: st = {}
                rh, rl = hi_prev, lo_prev
                base_dir = _bd_p
                ev += " (Range Inherit)"
                
                st["mode"] = "range-inherit"
                st["dir"] = int(base_dir)
                st["rh"] = rh
                st["rl"] = rl
                st["ev"] = ev
                
                self._pivot_state[tf] = st
            else:
                 # Break detected -> Sync
                 if st is None: st = {}
                 st["dir"] = int(base_dir)

                 # [FIX] Both Break History: Check Parent (M5)
                 if st["dir"] == 0:
                     p_dir, *_ = self._tf_dir("M5")
                     if p_dir is not None and int(p_dir) != 0:
                         st["dir"] = p_dir
                         ev += " (Ref:M5)"

                 st["rh"] = rh
                 st["rl"] = rl
                 st["ev"] = ev
                 st["mode"] = "break-sync"
                 self._pivot_state[tf] = st
                # Fall through...

        # --- H4/D1/W1：上位TFの Range-Inherit / Break-Sync (M15/H1/M5/M1 と同じパターン) ---
        _UPPER_TF_SET = {"H4", "D1", "W1"}
        _FALLBACK_PARENT = {"H4": "D1", "D1": "W1"}
        if tf in _UPPER_TF_SET and len(his) >= 2:
            opens_p  = [r["open"]  for r in his[:-1]]
            highs_p  = [r["high"]  for r in his[:-1]]
            lows_p   = [r["low"]   for r in his[:-1]]
            closes_p = [r["close"] for r in his[:-1]]
            _bd_p, rh_prev, rl_prev, _ev_prev = _adjdir_from_series(
                highs_p, lows_p, opens_p, closes_p, new_count=True
            )
            his_pre = his[:-1]
            _, rh_old, rl_old, _ = _adjdir_from_series(
                 [r["high"] for r in his_pre],
                 [r["low"] for r in his_pre],
                 [r["open"] for r in his_pre],
                 [r["close"] for r in his_pre],
                 new_count=True
            )
            prev_bar = his[-1]
            hi_prev = float(prev_bar["high"])
            lo_prev = float(prev_bar["low"])

            is_inside_range = False
            if rh_old is not None and rl_old is not None:
                 is_inside_range = (hi_prev <= float(rh_old) + 0.00001) and (lo_prev >= float(rl_old) - 0.00001)

            if is_inside_range:
                if st is None: st = {}
                rh, rl = hi_prev, lo_prev
                base_dir = _bd_p
                ev += " (Range Inherit)"
                st["mode"] = "range-inherit"
                st["dir"] = int(base_dir)
                st["rh"] = rh
                st["rl"] = rl
                st["ev"] = ev
                self._pivot_state[tf] = st
            else:
                if st is None: st = {}
                st["dir"] = int(base_dir)
                # 両抜け(dir==0)時は上位TFを参照
                if st["dir"] == 0:
                    pm = getattr(self, "profile", None).parent_map if getattr(self, "profile", None) else {}
                    ptf = pm.get(tf) or _FALLBACK_PARENT.get(tf)
                    if ptf:
                        p_dir, *_ = self._tf_dir(ptf)
                        if p_dir is not None and int(p_dir) != 0:
                            st["dir"] = int(p_dir)
                            ev += f" (Ref:{ptf})"
                st["rh"] = rh
                st["rl"] = rl
                st["ev"] = ev
                st["mode"] = "break-sync"
                self._pivot_state[tf] = st

        # Remove Guard (since we always have now from rates[-1])
        # If rates was stale, now is technically the Closed Bar, but we treat it as forming bar for logic consistency?
        # Use standard logic.
        
        # 以降：既存の方向判定／state管理ロジック（M1/M5は従来どおり）
        hi_now = float(now["high"])
        lo_now = float(now["low"])

        up_break = False
        down_break = False

        if st is None:
            # 初期ロック（確定足ベース）
            st = {"ev": ev, "dir": int(base_dir), "rh": rh, "rl": rl}
            if ev == "上げ止まり":
                st["dir"] = +1
            elif ev == "下げ止まり":
                st["dir"] = -1

            # 未確定バーで実ブレイクしていれば即反映
            rh_watch = rh
            rl_watch = rl
            if rh_watch is not None:
                up_break = hi_now >= float(rh_watch)
            if rl_watch is not None:
                down_break = lo_now <= float(rl_watch)

            if up_break and down_break:
                # ★追加：形成中の「両抜け」も上位足参照
                st["rh"], st["rl"] = rh, rl
                st["is_range"] = bool(is_range_prev)
                st["dir"] = 0
                display_ev = "両抜け(保留)"

                pm = getattr(self, "profile", None).parent_map if getattr(self, "profile", None) else {"M1": "M5", "M5": "M15", "M15": "H1"}
                parent_map = pm
                ptf = parent_map.get(tf)
                if ptf:
                    p_dir, _, _, _, _ = self._tf_dir(ptf)
                    if p_dir is not None and int(p_dir) != 0:
                        st["dir"] = 0
                        st["dir_override"] = int(p_dir)
                        st["dir_override_time"] = int(now["time"])
                        display_ev = f"両抜け(Ref:{ptf})"
                    elif int(base_dir) != 0:
                        # [FIX] 親TFも中立の場合: 確定足方向をフォールバック（親TFが存在するケースが抜けていた）
                        st["dir"] = int(base_dir)
                        display_ev = f"両抜け(Base:{base_dir:+d})"
                elif int(base_dir) != 0:
                    # 親TFなし: 確定足方向をフォールバック
                    st["dir"] = int(base_dir)
                    display_ev = f"両抜け(Base:{base_dir:+d})"
            elif st["dir"] == +1 and down_break:
                st = {"ev": "下げ止まり", "dir": -1, "rh": rh, "rl": rl}
            elif st["dir"] == -1 and up_break:
                st = {"ev": "上げ止まり", "dir": +1, "rh": rh, "rl": rl}

            st["is_range"]   = bool(is_range_prev)           # M15以外は False のまま
            st["just_broke"] = bool(up_break or down_break)

            self._log(
                f"[DIRDBG-boot] {tf} hi={hi_now:.5f} lo={lo_now:.5f} RH={rh} RL={rl} "
                f"dir0={st['dir']} range(prev)={st['is_range']}",
                tag=f"DIR.{tf}", level=1
            )
            self._pivot_state[tf] = st
            display_ev = st["ev"]

        else:
            # 形成中バーの実ブレイク監視
            rh_watch = st.get("rh", rh)
            rl_watch = st.get("rl", rl)
            if rh_watch is not None:
                up_break = hi_now >= float(rh_watch)
            if rl_watch is not None:
                down_break = lo_now <= float(rl_watch)

            if up_break and down_break:
                st["rh"], st["rl"] = rh, rl
                st["is_range"] = bool(is_range_prev)
                st["dir"] = 0
                display_ev = "両抜け(保留)"
                
                # ★追加：形成中の「両抜け」も上位足参照（stありの場合）
                pm = getattr(self, "profile", None).parent_map if getattr(self, "profile", None) else {"M1": "M5", "M5": "M15", "M15": "H1"}
                parent_map = pm
                ptf = parent_map.get(tf)
                if ptf:
                    p_dir, _, _, _, _ = self._tf_dir(ptf)
                    if p_dir is not None and int(p_dir) != 0:
                        st["dir"] = 0
                        st["dir_override"] = int(p_dir)
                        st["dir_override_time"] = int(now["time"])
                        display_ev = f"両抜け(Ref:{ptf})"
                    elif int(base_dir) != 0:
                        # [FIX] 親TFも中立の場合: 確定足方向をフォールバック（親TFが存在するケースが抜けていた）
                        st["dir"] = int(base_dir)
                        display_ev = f"両抜け(Base:{base_dir:+d})"
                elif int(base_dir) != 0:
                    # 親TFなし: 確定足方向をフォールバック
                    st["dir"] = int(base_dir)
                    display_ev = f"両抜け(Base:{base_dir:+d})"

            # ★ range-bar / range-inherit 専用：片側ブレイクで即反転
            elif st.get("mode") in ("range-bar", "range-inherit"):
                if down_break and not up_break:
                    st = {"ev": "下げ止まり", "dir": -1, "rh": rh, "rl": rl, "is_range": False}
                elif up_break and not down_break:
                    st = {"ev": "上げ止まり", "dir": +1, "rh": rh, "rl": rl, "is_range": False}
                else:
                    # ブレイクが無いなら range-bar を維持、監視情報だけ最新化
                    st["rh"], st["rl"] = rh, rl
                    st["is_range"] = bool(is_range_prev)
                display_ev = st.get("ev", ev)

            elif st["ev"] == "上げ止まり":
                if down_break:
                    st = {"ev": "下げ止まり", "dir": -1, "rh": rh, "rl": rl, "is_range": bool(is_range_prev)}
                else:
                    st["rh"], st["rl"] = rh, rl
                    st["is_range"] = bool(is_range_prev)
                display_ev = st["ev"]

            elif st["ev"] == "下げ止まり":
                if up_break:
                    st = {"ev": "上げ止まり", "dir": +1, "rh": rh, "rl": rl, "is_range": bool(is_range_prev)}
                else:
                    st["rh"], st["rl"] = rh, rl
                    st["is_range"] = bool(is_range_prev)
                display_ev = st["ev"]

            else:
                st = {"ev": ev, "dir": int(base_dir), "rh": rh, "rl": rl, "is_range": bool(is_range_prev)}
                display_ev = st["ev"]

            self._pivot_state[tf] = st
            st["just_broke"] = bool(up_break or down_break)

        d_eff = st["dir"]
        dir_override = st.get("dir_override")
        dir_override_time = st.get("dir_override_time")
        if dir_override is not None:
            if dir_override_time == int(now["time"]):
                d_eff = int(dir_override)
            else:
                st.pop("dir_override", None)
                st.pop("dir_override_time", None)
        if not hasattr(self, "_dir_base"): self._dir_base = {}
        if not hasattr(self, "_dir_eff"):  self._dir_eff  = {}
        self._dir_base[tf] = int(base_dir)
        self._dir_eff[tf]  = d_eff

        # ★v10: _bar_gate用に最新確定足のタイムスタンプを保存
        if not hasattr(self, "_last_tf_dir_bar_time"):
            self._last_tf_dir_bar_time = {}
        if len(his) > 0:
            self._last_tf_dir_bar_time[tf] = int(his[-1]["time"])

        result = (d_eff, st.get("rh", rh), st.get("rl", rl), display_ev, body_break)
        # Update per-bar cache (keyed by last closed bar time)
        bar_open_for_cache = int(his[-1]["time"]) if len(his) > 0 else 0
        self._tf_dir_cache[tf] = (bar_open_for_cache, result)
        return result



    # ── closed-bar gate helpers ─────────────────────────────────
    def _last_closed_bar_time(self, timeframe=mt5.TIMEFRAME_M5):
        """直近の“確定”バーの time（epoch秒）を返す。なければ None。"""
        rates = mt5.copy_rates_from_pos(self.symbol, timeframe, 0, 2)
        if rates is None or len(rates) < 2:
            return 0
        # rates[-1] は進行中のバー、rates[-2] が直近の確定バー
        return int(rates[-2]['time'])

    def _bar_gate(self, key: str, timeframe=mt5.TIMEFRAME_M5):
        """
        key: 'mae' or 'tflow' など。各キーごとに「その確定バーでは未処理なら True」を返す。
        True を返した時点で"そのバーは処理済み"にする。
        
        ★v10改善: _tf_dirで取得したバーのタイムスタンプを優先参照し、データ取得タイミングのズレを防ぐ。
        ★v10.5: 起動直後やプロファイル切り替え時は次の確定足まで待つ（即エントリー防止）
        """
        # タイムフレーム文字列へ変換
        tf_str_map = {
            mt5.TIMEFRAME_M1: "M1", mt5.TIMEFRAME_M5: "M5", mt5.TIMEFRAME_M15: "M15",
            mt5.TIMEFRAME_H1: "H1", mt5.TIMEFRAME_H4: "H4", mt5.TIMEFRAME_D1: "D1", mt5.TIMEFRAME_W1: "W1"
        }
        tf_str = tf_str_map.get(timeframe, "M5")
        
        # ★優先: _tf_dirで保存されたタイムスタンプを使用
        t = 0
        if hasattr(self, "_last_tf_dir_bar_time") and tf_str in self._last_tf_dir_bar_time:
            t = self._last_tf_dir_bar_time[tf_str]
        else:
            # フォールバック: 従来の方式
            t = self._last_closed_bar_time(timeframe)
        
        if not t:
            return False
        
        # TFごとに独立したゲートにする（例: pivot_entry_M5 / pivot_entry_M15）
        attr = f"_last_{key}_{tf_str}_bar_time"
        prev = getattr(self, attr, 0)
        
        # [FIX] 起動直後やプロファイル切り替え直後（prev=0）は次の確定足まで待つ
        if prev == 0:
            setattr(self, attr, t)
            self._log(f"[BAR_GATE] {key} initialized at {tf_str} bar {t}. Waiting for next bar.", level=2)
            return False  # 初回は次の確定足まで待つ
        
        if t <= prev:
            return False
        setattr(self, attr, t)
        return True



    # ── monitoring loop ──────────────────────────────────────
    def _monitor(self) -> None:
        print("[DEBUG-MONITOR] Monitor thread started!")
        print(f"[DEBUG-MONITOR] Symbol: {self.symbol}")

        # pt安全化
        info = mt5.symbol_info(self.symbol)
        pt = (info.point if info else 0.0) or 0.0
        print(f"[DEBUG-MONITOR] Got symbol info, pt={pt}")

        # ウォッチドッグ
        if not hasattr(self, "_last_watchdog"): self._last_watchdog = 0.0
        WD_SEC = 10.0

        # UI Budget Refresh Interval
        self._last_budget_ui_update = 0.0

        print("[DEBUG-MONITOR] Entering main monitoring loop...")
        self._tf_dir_cache = {}
        while self.running:
            try:
                time.sleep(CHECK_INTERVAL)

                # --- Invalidate per-iteration cache ---
                self._iter_tick = None
                self._iter_info = None
                self._iter_positions = None
                self._iter_orders = None
                self._monitor_hb_ts = time.time()

                # --- watchdog ---
                now_ts = time.time()
                if now_ts - self._last_watchdog >= WD_SEC:
                    self._last_watchdog = now_ts
                    # self._log("[WD] monitor alive", tag="WD", level=2)

                tick = mt5.symbol_info_tick(self.symbol)
                if not tick:
                    self._log("[WD] no tick / maybe disconnected", tag="WD", level=1)
                    time.sleep(1.0)
                    continue

                # --- Populate per-iteration cache ---
                self._iter_tick = tick
                self._iter_info = mt5.symbol_info(self.symbol)
                tick_time_sec = int(getattr(tick, "time", 0) or 0)
                self._last_tick_time = tick_time_sec or int(time.time())
                prev_tick_time_sec = int(getattr(self, "_last_tick_time_sec", 0) or 0)
                if tick_time_sec > 0 and tick_time_sec == prev_tick_time_sec:
                    if getattr(self, "_stale_tick_start", None) is None:
                        self._stale_tick_start = now_ts
                else:
                    self._last_tick_time_sec = tick_time_sec
                    self._stale_tick_start = None
                    self._market_closed_stale = False

                # ── Market Closed / Idle Check ──
                # 閉場時（週末やメンテ）の無駄な処理を抑制
                # 1) Tickが古い (例: 30分以上前)
                # 2) 同じtick.timeが一定時間続く (市場閉鎖中のキャッシュtick対策)
                # 3) トレードモードが禁止
                is_idle = False
                
                # ── New Bar Detection (Dynamic) ──
                # 1ポジ目のエントリータイミング精緻化のため 新規足を検出
                is_new_m1  = self._check_new_bar(mt5.TIMEFRAME_M1, tick=tick)
                is_new_m5  = self._check_new_bar(mt5.TIMEFRAME_M5, tick=tick)
                is_new_m15 = self._check_new_bar(mt5.TIMEFRAME_M15, tick=tick)
                
                # Expose to logic
                self.is_new_m1_bar  = is_new_m1
                self.is_new_m5_bar  = is_new_m5
                self.is_new_m15_bar = is_new_m15
                # ★v10.4: Ensure all 5 profile timeframes are calculated for GUI/Logic
                # This ensures H4/D1/W1 etc are updated even if not used in primary logic
                # [FIX] Wrap in try/except to prevent any single TF from blocking the loop
                if hasattr(self, "_profile_tf_list"):
                    for tf_c in self._profile_tf_list:
                        try:
                            tf_s = self._get_tf_str(tf_c)
                            # Call _tf_dir to update state (side effect)
                            self._tf_dir(tf_s)
                        except Exception:
                            # Skip this TF if it fails/times out, don't block the whole loop
                            pass

                # ★v10.6: Zigzag Entry Permission State Update
                try:
                    self._update_zigzag_state()
                except Exception:
                    pass

                # ★v10.6: Nanpin Hedge + NP Guard GUI
                # GUI表示は毎tick更新、実際のヘッジ判定はM1確定時のみ
                try:
                    self._refresh_hedge_gui()
                    self._refresh_np_guard_gui()
                except Exception:
                    pass
                if is_new_m1:
                    try:
                        self._check_nanpin_hedge()
                    except Exception:
                        pass

                try:
                    if (now_ts - float(tick.time)) > 1800.0:
                        is_idle = True
                    else:
                        stale_age = 0.0
                        stale_start = getattr(self, "_stale_tick_start", None)
                        if stale_start is not None:
                            stale_age = now_ts - float(stale_start)
                        if stale_age >= float(MARKET_STALE_TICK_SEC):
                            is_idle = True
                            self._market_closed_stale = True
                        else:
                            self._market_closed_stale = False
                        s_info = self._get_cached_info()
                        if (not is_idle) and s_info and s_info.trade_mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
                            is_idle = True
                except Exception:
                    pass

                if is_idle:
                    self.is_new_m1_bar = False # Idle時はフラグクリア
                    self.is_new_m5_bar = False
                    self.is_new_m15_bar = False
                    if (now_ts - getattr(self, "_last_idle_log", 0.0)) > 300.0:
                        self._last_idle_log = now_ts
                        if bool(getattr(self, "_market_closed_stale", False)):
                            self._set_status("Market Closed / Stale Tick")
                            self._log(
                                f"[WD] stale tick detected: tick.time={tick_time_sec} "
                                f"unchanged for {max(0.0, now_ts - float(getattr(self, '_stale_tick_start', now_ts))):.0f}s",
                                tag="WD",
                                level=1,
                            )
                        else:
                            self._set_status("Market Closed / Idle")
                    
                    time.sleep(5.0)
                    continue

                mid_now = (tick.bid + tick.ask) / 2

                # ── Soft Close (Market Closing Block) ────────────
                # サーバー時間をチェックし、クローズ間際なら「market_closing_block」を立てる
                # ※Broker時間の hour を参照
                try:
                    import datetime
                    dt_server = datetime.datetime.fromtimestamp(tick.time) # timezone naive (local/server representation)
                    # weekday: 0=Mon ... 4=Fri ... 6=Sun
                    # 金曜の指定時間以降 OR 毎日の指定時間以降
                    is_weekend_close = (dt_server.weekday() == 4 and dt_server.hour >= WEEKEND_BLOCK_HOUR)
                    is_daily_close   = (dt_server.hour >= DAILY_BLOCK_HOUR)
                    
                    if is_weekend_close or is_daily_close:
                        self.market_closing_block = True
                        if (now_ts - getattr(self, "_last_close_log", 0.0)) > 60.0:
                            self._last_close_log = now_ts
                            self._set_status(f"Closing Block active (Server {dt_server.strftime('%H:%M')})")
                    else:
                        self.market_closing_block = False
                except Exception:
                    self.market_closing_block = False

                # ── UI Update (Periodic) ─────────────────────────
                # Budget表示が "entry check時" にしか更新されない問題を修正
                # 定期的に (例: 1秒ごと) 表示リフレッシュ
                if (now_ts - getattr(self, "_last_budget_ui_update", 0.0)) > 1.0:
                    self._last_budget_ui_update = now_ts
                    # 副作用のみ利用（戻り値無視）
                    self._entry_budget_check("buy")
                    self._entry_budget_check("sell")

                # ── Step Recalculation (Periodic) ────────────────
                # 朝スプなどで広がった step_pts がそのまま維持されてしまうのを防ぐため、定期更新する
                if (now_ts - getattr(self, "_last_step_recalc_ts", 0.0)) > 5.0:
                    self._last_step_recalc_ts = now_ts
                    # info は冒頭で取得済みだが、古い可能性もあるので fresh fetch 推奨だが
                    # ここでは軽量化のため既存 info (wdループ毎ではないが.. loop冒頭はWD checkのみ)
                    # いや、monitor loop直下で tick 取得してるなら info もリフレッシュすべき
                    info_fresh = self._get_cached_info()
                    if info_fresh:
                        self.step_pts = self._compute_step_pts(tick, info_fresh)

                # === 外部決済（SL/TP/手動）検出：pivot heat の基準更新 ===
                self._detect_close_event_from_positions()
                # keep NANPIN lock state updated (legacy total-profit path is disabled)
                self._sync_nanpin_lock_from_positions()
                # keep mode label synced to live position state
                self._sync_mode_status_from_positions()
                # snapshot for GUI thread (avoid direct MT5 calls from tkinter callbacks)
                try:
                    self._last_positions_snapshot = list(self._get_cached_positions())
                except Exception:
                    pass

                # [NEW] Smart Profile State Maintenance
                if getattr(self, "_smart_profile_enable", False):
                     # [FIX] Isolation
                     poss_monitor = self._get_cached_positions()
                     pos_cnt = len(poss_monitor)
                     current_smart = getattr(self, "_smart_state", "STANDBY")
                     
                     # Check Transition: STANDBY -> ACTIVE (Day/M5 detection)
                     # Swing/M15 detection is handled in the probe block above, but M5 entry happens in main _pivot_entry_check
                     if current_smart == "STANDBY" and pos_cnt > 0:
                          # Assuming M5 (Day) entry since M15 probe handles its own transition
                          self._smart_state = "ACTIVE_M5"
                          self._smart_base_profile = "Day (M5)"
                          self._log("[SMART] State transition: STANDBY -> ACTIVE_M5 (detected position)", level=1)
                          self._set_status("Smart: Active (Day)")
                          
                     # Check Transition: ACTIVE -> NANPIN
                     # Triggered by _nanpin_lock (updated by sync helper / early-release logic)
                     if "ACTIVE" in current_smart and getattr(self, "_nanpin_lock", False):
                          self._smart_state = "NANPIN"
                          self._log("[SMART] State transition: ACTIVE -> NANPIN (Recovery Mode)", level=1)
                          self._set_status("Smart: Recovery (M1)")
                          
                          # Ensure profile switches to Scalp (M1) when entering NANPIN
                          try:
                              if self.profile.name != "Scalp (M1)":
                                  if not hasattr(self, "_nanpin_original_profile") or not self._nanpin_original_profile:
                                      self._nanpin_original_profile = self.profile.name
                                  target = "Scalp (M1)"
                                  if target in self.profiles:
                                      self.profile = self.profiles[target]
                                      if hasattr(self.profile, "cd_sec"): self.pivot_cooldown_sec = float(self.profile.cd_sec)
                                      if hasattr(self.profile, "hold_sec"): self.term_min_hold_sec = float(self.profile.hold_sec)
                                      if not self.headless and getattr(self, "_mon_vars", None):
                                          self._update_gui_labels()
                                      self._log(f"[NANPIN] Smart switch to {target} (from {self._nanpin_original_profile})", level=1)
                          except Exception as e:
                              self._log(f"[NANPIN] Smart switch error: {e}", level=2)

                     # Check Transition: ACTIVE/NANPIN -> STANDBY (All Closed)
                     if current_smart != "STANDBY" and pos_cnt == 0:
                          self._smart_state = "STANDBY"
                          self._smart_base_profile = None
                          self._nanpin_lock = False # Ensure clear
                          
                          # Reset Profile to Day (M5) for monitoring
                          target = "Day (M5)"
                          if target in self.profiles and self.profile.name != target:
                               self.profile = self.profiles[target]
                               self._log(f"[SMART] All closed. Resetting to {target}", level=1)
                               # Re-apply params
                               if hasattr(self.profile, "cd_sec"): self.pivot_cooldown_sec = float(self.profile.cd_sec)
                               if hasattr(self.profile, "hold_sec"): self.term_min_hold_sec = float(self.profile.hold_sec)

                          # [FIX] GUIのTF Modeラベルを即時同期（SCAのまま残る問題を修正）
                          self._update_gui_labels()
                          # [FIX] 次サイクルのnanpin開始時にoriginal profileを正しく保存できるようリセット
                          self._nanpin_original_profile = None
                          # [FIX] ヘッジ関連フラグもリセット
                          self._nanpin_hedge_done = False
                          self._nanpin_hedge_vol = 0.0
                          self._nanpin_hedge_last_ts = 0.0
                          try: self._update_hedge_gui()
                          except Exception: pass

                          self._log("[SMART] State transition: -> STANDBY", level=1)
                          self._set_status(f"Smart: Standby ({target})")

                # === 1) Smart Close (4段階決済) ===
                # 既存の全決済・Pair-Net・Profit Recycling は Smart Close に統合
                # _check_total_profit_threshold_and_close / _check_pair_profit_and_close は無効化
                if self._check_smart_close():
                    continue

                # === 1-LEGACY) グローバル/コンボの利益優先クローズ [DISABLED by Smart Close] ===
                # if self._check_total_profit_threshold_and_close():
                #     continue

                # === 2) Pair-Net 相殺 [DISABLED by Smart Close] ===
                n = 0
                # if not bool(getattr(self, "_offset_enable_flag", True)):
                #     n = 0
                # else:
                #     n = self._check_pair_profit_and_close()

                # === 2-B) Profit Recycling [DISABLED by Smart Close] ===
                # if n == 0:
                #     if not bool(getattr(self, "_offset_enable_flag", True)):
                #         pass
                #     else:
                #         n = self._exec_profit_recycling()

                # [NEW] Global Early Release Trigger (RL/RH Break -> Nanpin)
                # Check regardless of profile (but usually applies to Day/Swing active states)
                # If we are NOT yet in nanpin lock, and we have positions, check RL/RH
                if not getattr(self, "_nanpin_lock", False):
                     self._check_early_release_trigger()

                # 相殺で offset-entry を積んだ直後に1件だけ再試行
                self._process_offset_retry_queue()

                if n:
                    continue

                # === 3) Recenter（新規より前に） ===
                if self.grid_enable and self.step_pts and abs(mid_now - (self.mid or mid_now)) >= self.step_pts * pt:
                    now2 = time.time()
                    if now2 - self.last_recent_ts >= self.recenter_cooldown:
                        self.last_recent_ts = now2
                        self._recenter(mid_now)

                # Pivot一致エントリ
                if getattr(self, "pivot_enable", True):
                    # [NEW] Smart Profile Standby: Check Secondary Trigger (Swing M15) implicitly
                    # If M15 bar fits, temporarily switch profile to Swing, check, and revert if no entry.
                    if getattr(self, "_smart_profile_enable", False) and getattr(self, "_smart_state", "IDLE") == "STANDBY":
                         if self.is_new_m15_bar:
                             # Try Swing
                             cur_prof = self.profile
                             tgt_name = "Swing (M15)"
                             if tgt_name in self.profiles and cur_prof.name != tgt_name:
                                 self.profile = self.profiles[tgt_name]
                                 # self._log(f"[SMART] Probing {tgt_name} trigger...", level=1)
                                 try:
                                     self._pivot_entry_check() # Will fire if logic ok
                                     
                                     # Check if we entered (naive check by pos count, might lag but sufficient for 1st entry)
                                     # [FIX] Isolation
                                     now_poss = self._get_cached_positions()
                                     if len(now_poss) > 0:
                                         self._smart_state = "ACTIVE_M15"
                                         self._smart_base_profile = tgt_name
                                         self._log(f"[SMART] HIT on {tgt_name}! Switching state to ACTIVE_M15.", level=1)
                                         self._set_status(f"Smart: Active ({tgt_name})")
                                         # DO NOT REVERT PROFILE
                                         cur_prof = None 
                                 except Exception as e:
                                     self._log(f"[SMART] Swing probe error: {e}", level=2)
                                 
                                 # Revert if not hit
                                 if cur_prof:
                                     self.profile = cur_prof

                    self._pivot_entry_check() # Standard check (Day/M5 or Active Profile)

                # === 4) 新規：TFlow → MAE ===
                sig_tf = self._tflow_signal()
                if sig_tf:
                    self._tflow_fire(sig_tf)

                sig_mae = None
                if getattr(self, 'mae_enable', False) and hasattr(self, '_mae_signal'):
                    try:
                        sig_mae = self._mae_signal()
                    except Exception as e:
                        self._log(f"MAE signal error: {e}", level=2, tag="MAE")
                if sig_mae and hasattr(self, '_mae_fire'):
                    try:
                        self._mae_fire(sig_mae)
                    except Exception as e:
                        self._log(f"MAE fire error: {e}", level=2, tag="MAE")

                # === 5) offset-entry 再試行（2回目） ===
                self._process_offset_retry_queue()

                # === 6) GUIモニタ更新（高負荷のため1秒に1回に間引く） ===
                if (now_ts - getattr(self, "_last_gui_stat_update", 0.0)) > 1.0:
                    self._last_gui_stat_update = now_ts
                    # _debug_log(f"[MON] Loop Alive. price={tick.bid if tick else 'NA'}")
                    try:
                        info = self._get_cached_info(); pt = (info.point if info else 0.0) or 0.0
                        tick = self._get_cached_tick()
                        spread_pts = int(round((tick.ask - tick.bid)/pt)) if (tick and pt) else int((info.spread if info else 0) or 0)
                        
                        # ★ Spread Auto-Update
                        if getattr(self, "spread_auto_mode", False):
                            self._update_spread_stats(spread_pts)
                        
                        self._safe_set(self._mon_vars["spread"], str(spread_pts))
                        self._safe_set(self._mon_vars["step"], str(int(self.step_pts or 0)))

                        # [FIX] Isolation
                        poss = self._get_cached_positions()

                        # [FIX] Filter by Magic Number (Isolation for Unified Mode)
                        magic = int(getattr(self, "magic", 0))
                        if magic != 0:
                            poss = [p for p in poss if int(getattr(p, "magic", 0)) == magic]

                        b_all = sum(1 for p in poss if p.type == mt5.POSITION_TYPE_BUY)
                        s_all = sum(1 for p in poss if p.type == mt5.POSITION_TYPE_SELL)

                        b_tfl = s_tfl = b_mae = s_mae = 0
                        for p in poss:
                            c = (getattr(p, "comment", "") or "").lower()
                            if "tflow" in c or "tfl" in c:
                                if p.type == mt5.POSITION_TYPE_BUY: b_tfl += 1
                                else: s_tfl += 1
                            elif "mae" in c:
                                if p.type == mt5.POSITION_TYPE_BUY: b_mae += 1
                                else: s_mae += 1
                        if (b_tfl + s_tfl) == 0:
                            b_tfl, s_tfl = b_all, s_all
                        if (b_mae + s_mae) == 0:
                            b_mae = s_mae = 0

                        self._safe_set(self._mon_vars["tfl_live"], f"{b_tfl}/{s_tfl}")
                        self._safe_set(self._mon_vars["mae_live"], f"{b_mae}/{s_mae}")

                        # [FIX] Isolation
                        ords = self._get_cached_orders()
                        pb = ps = 0
                        plb = pls = 0.0
                        for o in ords:
                            t = getattr(o, "type", None)
                            vol = float(getattr(o, "volume_current", 0.0) or getattr(o, "volume_init", 0.0) or 0.0)
                            if t in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP, mt5.ORDER_TYPE_BUY_STOP_LIMIT, getattr(mt5, "ORDER_TYPE_BUY", 2)):
                                pb += 1; plb += vol
                            elif t in (mt5.ORDER_TYPE_SELL_LIMIT, mt5.ORDER_TYPE_SELL_STOP, mt5.ORDER_TYPE_SELL_STOP_LIMIT, getattr(mt5, "ORDER_TYPE_SELL", 3)):
                                ps += 1; pls += vol
                        self._safe_set(self._mon_vars["pend_b"], f"{pb}/{plb:.2f}")
                        self._safe_set(self._mon_vars["pend_s"], f"{ps}/{pls:.2f}")
                        self._safe_set(self._mon_vars["pend_tot"], f"{pb+ps}/{(plb+pls):.2f}")

                        b_cnt = s_cnt = 0
                        b_lot = s_lot = 0.0
                        b_pnl = s_pnl = 0.0
                        for p in poss:
                            vol = float(getattr(p, "volume", 0.0) or 0.0)
                            pnl = float(getattr(p, "profit", 0.0) or 0.0)
                            if p.type == mt5.POSITION_TYPE_BUY:
                                b_cnt += 1; b_lot += vol; b_pnl += pnl
                            else:
                                s_cnt += 1; s_lot += vol; s_pnl += pnl
                        self._safe_set(self._mon_vars["pos_b"], f"{b_cnt}/{b_lot:.2f}/{b_pnl:+.2f}")
                        self._safe_set(self._mon_vars["pos_s"], f"{s_cnt}/{s_lot:.2f}/{s_pnl:+.2f}")
                        self._safe_set(self._mon_vars["pos_net"], f"{(b_lot+s_lot):.2f}/{(b_pnl+s_pnl):+.2f}")

                        if (b_pnl > s_pnl): ws_pnl = "BUY"
                        elif (s_pnl > b_pnl): ws_pnl = "SELL"
                        else: ws_pnl = "TIE"
                        if (b_lot < s_lot): ws_exp = "BUY(minority)"
                        elif (s_lot < b_lot): ws_exp = "SELL(minority)"
                        else: ws_exp = "TIE"
                        self._safe_set(self._mon_vars["winside_pnl"], f"PnL: {ws_pnl}")
                        self._safe_set(self._mon_vars["winside_expo"], f"Expo: {ws_exp}")
                    except Exception as e:
                        # GUI集計は落としても監視は継続
                        # ★DEBUG: エラー内容を表示 (5秒に1回)
                        now_err = time.time()
                        if (now_err - getattr(self, "_last_gui_err_ts", 0.0)) > 5.0:
                            self._last_gui_err_ts = now_err
                            self._log(f"[GUI UPDATE ERROR] {e}", level=1)
                        pass

                    # 補助UIもここで更新
                    self._refresh_pivot_ui()
                    self._refresh_tf_dir_ui()
                    # state log（ラベル付け用）
                    self._state_log_row()

                # === 7) TP制御（最後にまとめて） ===
                self._tp_control()

            except Exception as e:
                # どこかで例外→ループ継続（スレッドを生かす）
                import traceback
                self._log(f"[MON] loop error: {e}\n{traceback.format_exc()}", tag="MON", level=2)
                continue

    # ── [NEW] Global Early Release Trigger ──────────────────────
    def _check_early_release_trigger(self):
        """
        Check if current price broke the RL (for Buy) or RH (for Sell) of the active profile's execution TF.
        If triggers, force transition to Nanpin Mode.
        """
        try:
            # 1. Check Positions
            # [FIX] Isolation
            poss = self._get_cached_positions()
            if not poss: return

            # Filter by Magic (if strict isolation enabled)
            magic = int(getattr(self, "magic", 0))
            if magic != 0:
                poss = [p for p in poss if int(getattr(p, "magic", 0)) == magic]
            
            if not poss: return

            # 2. Get Net Direction
            b_cnt = sum(1 for p in poss if p.type == mt5.POSITION_TYPE_BUY)
            s_cnt = sum(1 for p in poss if p.type == mt5.POSITION_TYPE_SELL)
            
            target_side = None
            if b_cnt > s_cnt: target_side = "BUY"
            elif s_cnt > b_cnt: target_side = "SELL"
            else: return # Neutral/Equal -> No clear direction to protect

            # 3. Get Current Profile Exec TF
            prof = self.profile
            exec_tf_str = getattr(prof, "exec_str", "M5") 
            # Fallback if property missing
            if not hasattr(prof, "exec_str"):
                # Manual map
                tm = {mt5.TIMEFRAME_M1:"M1", mt5.TIMEFRAME_M5:"M5", mt5.TIMEFRAME_M15:"M15", mt5.TIMEFRAME_H1:"H1", mt5.TIMEFRAME_H4:"H4", mt5.TIMEFRAME_D1:"D1"}
                exec_tf_str = tm.get(getattr(prof, "exec_tf", mt5.TIMEFRAME_M5), "M5")

            # 4. Get RL/RH
            d, rh, rl, ev, bb = self._tf_dir(exec_tf_str)
            
            # 5. Check Break
            tick = self._get_cached_tick()
            if not tick: return
            
            triggered = False
            msg = ""
            
            if target_side == "BUY":
                # Protecting Buy: Support is RL. Break if Bid < RL
                # Should check if RL is valid
                if rl is not None:
                     if tick.bid < float(rl):
                         triggered = True
                         msg = f"[Early Release] BUY pos broken RL({rl}) on {exec_tf_str}. Bid={tick.bid}"
            elif target_side == "SELL":
                 # Protecting Sell: Resistance is RH. Break if Ask > RH
                 if rh is not None:
                      if tick.ask > float(rh):
                           triggered = True
                           msg = f"[Early Release] SELL pos broken RH({rh}) on {exec_tf_str}. Ask={tick.ask}"

            if triggered:
                 self._log(msg, level=1)
                 self._nanpin_lock = True
                 # v10.6: ヘッジはM1確定時に _check_nanpin_hedge で判定
                 self._set_status("Early Release: NANPIN Mode Triggered")
                 
                 # Force Smart Profile State Update if enabled
                 if getattr(self, "_smart_profile_enable", False):
                      self._smart_state = "NANPIN"
                      self._log("[SMART] State forced to NANPIN (Early Release)", level=1)

        except Exception as e:
            self._log(f"[EarlyCheck] error: {e}", level=2)

    def _sync_nanpin_lock_from_positions(self) -> None:
        """
        Keep nanpin lock in sync even when legacy total-profit loop is disabled.
        - Hedge state (both buy/sell) -> lock ON
        - Flat -> lock OFF
        - One-sided while locked -> unlock only after small recovery
        """
        try:
            prev_lock = bool(getattr(self, "_nanpin_lock", False))
            poss = self._get_cached_positions()
            if not poss:
                self._nanpin_lock = False
                if prev_lock:
                    self._log("[SMART] NANPIN lock OFF (flat)", level=1)
                return

            b_cnt = sum(1 for p in poss if p.type == mt5.POSITION_TYPE_BUY)
            s_cnt = sum(1 for p in poss if p.type == mt5.POSITION_TYPE_SELL)

            if b_cnt > 0 and s_cnt > 0:
                self._nanpin_lock = True
                if not prev_lock:
                    self._log(f"[SMART] NANPIN lock ON (hedged b={b_cnt} s={s_cnt})", level=1)
                return

            if self._nanpin_lock:
                t_prof = sum(float(getattr(p, "profit", 0.0) or 0.0) for p in poss)
                u_th = max(0.5, float(globals().get("TOTAL_PROFIT_THRESHOLD", 0.1)) + 0.1)
                if t_prof > u_th:
                    self._nanpin_lock = False
                    self._log(f"[SMART] NANPIN lock OFF (recovered total={t_prof:.2f} > {u_th:.2f})", level=1)
        except Exception:
            pass

    def _sync_mode_status_from_positions(self) -> None:
        """Sync mode label with legacy-style mode judgment (without legacy close side effects)."""
        try:
            poss = self._get_cached_positions()
            if not poss:
                ms = "IDLE"
                self._is_pyramid_mode = False
            else:
                is_hold_period = False
                is_pure_trend_hold = False

                # Legacy preserve-only state label.
                is_only_preserve = False
                try:
                    preserved_tickets = self._get_preserve_tickets(poss)
                    non_preserved_poss = [p for p in poss if p.ticket not in preserved_tickets]
                    is_only_preserve = (len(poss) > 0 and len(non_preserved_poss) == 0)
                except Exception:
                    is_only_preserve = False

                hold_sec_logic = float(getattr(self.profile, "hold_sec", self.term_min_hold_sec))
                buy_vol = sum(p.volume for p in poss if p.type == mt5.POSITION_TYPE_BUY)
                sell_vol = sum(p.volume for p in poss if p.type == mt5.POSITION_TYPE_SELL)
                total_vol = buy_vol + sell_vol
                total_cost = sum(p.volume * p.price_open for p in poss)
                avg_price_calc = (total_cost / total_vol) if total_vol > 0 else 0.0
                is_one_sided = (buy_vol > 0 and sell_vol == 0) or (sell_vol > 0 and buy_vol == 0)

                if len(poss) == 1:
                    p = poss[0]
                    tick = self._get_cached_tick()
                    if tick:
                        elapsed_sec = (float(tick.time_msc) - float(p.time_msc)) / 1000.0
                        if elapsed_sec <= hold_sec_logic:
                            is_hold_period = True
                            is_pure_trend_hold = True
                        else:
                            base_th = float(globals().get("TOTAL_PROFIT_THRESHOLD", 0.1))
                            buffer_val = max(0.5, base_th + 0.1)
                            is_pure_trend_hold = float(getattr(p, "profit", 0.0) or 0.0) > buffer_val
                elif is_one_sided and (not bool(getattr(self, "_nanpin_lock", False))):
                    by_ticket = sorted(poss, key=lambda p: p.ticket)
                    if by_ticket:
                        first_open = by_ticket[0].price_open
                        base_th = float(globals().get("TOTAL_PROFIT_THRESHOLD", 0.1))
                        buffer_val = max(0.5, base_th + 0.1)
                        total_profit = sum(float(getattr(p, "profit", 0.0) or 0.0) for p in poss)
                        if buy_vol > 0 and avg_price_calc >= first_open:
                            is_pure_trend_hold = total_profit > buffer_val
                        elif sell_vol > 0 and avg_price_calc <= first_open:
                            is_pure_trend_hold = total_profit > buffer_val

                if is_only_preserve:
                    ms = "PYRAMID (Preserve)"
                    self._is_pyramid_mode = True
                elif is_hold_period:
                    ms = f"HOLD ({int(hold_sec_logic)}s)"
                    self._is_pyramid_mode = True
                elif is_pure_trend_hold:
                    ms = "PYRAMID (Trend)"
                    self._is_pyramid_mode = True
                else:
                    ms = "NANPIN (Recovery)"
                    self._is_pyramid_mode = False
            self._current_mode_str = ms
            if not self.headless and getattr(self, "_mon_vars", None):
                self._safe_set(self._mon_vars.get("mode_status"), ms)
        except Exception:
            pass


    # ── cancel orders & close positions ──────────────────────
    def _full_close(self) -> None:
        # [FIX] Use isolated helpers
        for o in self._get_my_orders():
            if hasattr(mt5, "order_delete"): mt5.order_delete(o.ticket)
            else: self._order_send_with_retry({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket, "symbol": o.symbol})
        for p in self._get_my_positions():
            self._market_close(p, p.volume)
        # ★Fix: 手動全決済時はOffset状態もリセットする
        self._reset_offset_state()
        self._set_status("All closed.")

    def _drain_ui_queue(self) -> None:
        """GUI更新をメインスレッドに集約する簡易ディスパッチ"""
        if self.headless or not getattr(self, "_ui_queue", None) or not getattr(self, "root", None):
            return
        try:
            while True:
                fn = self._ui_queue.get_nowait()
                try:
                    fn()
                except Exception as e:
                    self._log(f"[UI] dispatch error: {e}", level=1)
        except queue.Empty:
            pass
        try:
            self.root.after(50, self._drain_ui_queue)
        except Exception:
            pass

    # ── status helper ────────────────────────────────────────
    def _safe_set(self, var, value) -> None:
        """Thread-safe GUI variable update（headless対応）"""
        if self.headless or var is None:
            try:
                var.set(value)
            except Exception:
                pass
            return
        # Unifiedモードでの多スレッド更新を抑制するため、UIキュー経由でメインスレッドに委譲
        if getattr(self, "_ui_queue", None) is not None and getattr(self, "root", None):
            try:
                self._ui_queue.put_nowait(lambda v=value, t=var: t.set(v))
            except Exception:
                pass
            return
        if getattr(self, "root", None):
            try:
                self.root.after(0, lambda v=value, t=var: t.set(v))
            except Exception:
                pass

    def _set_status(self, msg: str) -> None:
        if STATUS_PRINT_STDOUT or self.headless:
            print("[STATUS]", msg)
        if self.headless: self.status = msg
        else:
            # Tcl_AsyncDelete Crash Fix: Schedule GUI update on main thread
            self._safe_set(self.status, msg)

    def _update_spread_stats(self, cur_pts: float):
        """スプレッドのEMAを更新し、許容最大値を動的にセットする"""
        if self._spread_ema is None:
            self._spread_ema = float(cur_pts)
        else:
            alpha = SPREAD_FILTER_EMA_ALPHA
            self._spread_ema = alpha * cur_pts + (1 - alpha) * self._spread_ema
            
        # Update dynamic max
        new_max = max(SPREAD_FILTER_MIN_PTS, self._spread_ema * SPREAD_FILTER_MULTIPLIER)
        self.spread_max_pts = int(new_max)


    # ── GUI: abort button ────────────────────────────────────
    def _abort(self) -> None:
        if self.headless:
            self.running = False; self._full_close()
        else:
            if messagebox.askyesno("Abort", "Stop trading and exit?", parent=self.root):
                self.running = False; self._full_close()
                # [FIX] Unified Mode: Do NOT kill root (app)
                if not getattr(self, "gui_parent", None):
                    self.root.after(500, self.root.quit)

    # ── run bot ──────────────────────────────────────────────
    def _toggle_pause(self) -> None:
        """一時停止と再開を切り替える"""
        self.trading_paused = not self.trading_paused
        txt = _t("ui.btn.resume") if self.trading_paused else _t("ui.btn.pause")
        if hasattr(self, "pause_btn"):
            self.pause_btn.config(text=txt)
        status = "PAUSED" if self.trading_paused else "RUNNING"
        self._log(f"[SYSTEM] Trading {status}", level=1)
        self._set_status(f"Trading {status}")

    # ── Persistence Methods ──
    def _apply_runtime_mult(self):
        """GUIの適用ボタン押下時に呼ばれる。現在の倍率を更新し永続化。"""
        try:
            val = float(self._mon_vars["spread_mult_rt"].get())
            if val < 1.0: val = 1.0
            self.spread_mult = val
            self._log(f"[UI] Runtime Spread Mult changed to {val:.2f}", level=1)
            self._save_current_config()
            self._set_status(f"Mult applied: {val:.2f}")
        except Exception as e:
            if not self.headless:
                messagebox.showerror("Error", f"Invalid multiplier: {e}", parent=self.root)

    def _save_current_config(self):
        """現在のインスタンス状態をJSONに永続化"""
        cfg = {
            "symbol": self.symbol,
            "digits": self.digits,
            "lot": self.lot,
            "side": self.side,
            "spread_mult": self.spread_mult,
            "pair_profit_th": getattr(self, "pair_profit_threshold", 0.5),
            "mae_cd": self.mae_cd_sec,
            "tflow_cd": self.tflow_cd_sec,
            "grid_enable": self.grid_enable,
            "mae_enable": self.mae_enable,
            "tflow_enable": self.tflow_enable,
            "grid_mode": self.grid_mode,
            "strict": getattr(self, "strict_pending_cleanup", True),
            "keep_nearest": getattr(self, "keep_nearest_slots", 0),
            "flip_ratio": getattr(self, "exposure_flip_ratio", 0.12),
            "flip_cd": getattr(self, "winside_flip_cooldown_sec", 10),
            "min_dist_mult": getattr(self, "min_entry_distance_mult", 0.6),
            "tfl_live": self.tflow_max_live,
            "tfl_mult": self.tflow_lot_mult,
            "mae_live": self.mae_max_live,
            "mae_mult": self.mae_lot_mult,
            "pivot_enable": getattr(self, "pivot_enable", True),
            "pivot_cd": getattr(self, "pivot_cd_sec", 119.0),
            "pivot_skip_c1": getattr(self, "pivot_strict_skip_c1", False),
            "discord_enable": self._discord_enable_flag,
            "discord_url": self.discord_url,
            "volatility_enable": self.volatility_guard_enable,
            "volatility_mult": self.volatility_atr_mult,
            "block_time_enable": self.block_time_enable,
            "block_time_start": self.block_time_start_hour,
            "block_time_end": self.block_time_end_hour,
            "profile": getattr(self.profile, "name", "Scalp (M1)"),
            "req_ref2_bb": getattr(self, "pivot_first_require_ref2_body_break", False),
            "req_upper_bb": getattr(self, "pivot_first_require_upper_body_break", True),
            "zigzag_gate": getattr(self, "pivot_zigzag_entry_enable", False),
            "nanpin_hedge": getattr(self, "nanpin_full_hedge_enable", False),
            "nanpin_prevent": getattr(self, "nanpin_prevent_enable", False)
        }
        _save_config_to_disk(self.symbol, cfg)
    def _save_profile_state(self):
        """Save current profile name to disk"""
        try:
            path = _get_profile_path(self.symbol)
            data = {"profile": self.profile.name, "updated": time.time()}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception as e:
            self._log(f"[Persistent] Failed to save profile: {e}", level=2)

    def run(self) -> None:
        self._mt5_init()
        # ML gate（任意）
        try:
            self._ai_load_model()
        except Exception:
            pass
        try:
            self._log(f"[AI] log_file={self.ai_log_file}", tag="AI", level=1)
            if not self.headless and getattr(self, "_mon_vars", None):
                self._safe_set(self._mon_vars["entry_budget"], f"log: {os.path.basename(self.ai_log_file)}")
        except Exception:
            pass
        # 初期ビルド
        try:
            acc = mt5.account_info()
            login = getattr(acc, "login", None)
            sym = self.symbol
            head_txt = f"Account: {login}   |   Symbol: {sym}"
            if not self.headless and getattr(self, "root", None):
                # [FIX] Thread Safety: Do NOT update title from background thread (Unified Mode child)
                if not getattr(self, "gui_parent", None):
                    try:
                        self.root.title(f"{sym} | {login} | Slot Grid + PairNet")
                    except Exception:
                        pass
                try:
                    self._safe_set(self.header, head_txt)
                except Exception:
                    pass
            else:
                # headless の場合は stdout にも出しておく
                print(head_txt)
        except Exception:
            pass        
        if self.grid_enable:
            self._build_grid()
            # Update status after grid initialization completes
            self._set_status(f'Ready (Grid enabled, mode={self._current_mode_str})')
        else:
            tick = mt5.symbol_info_tick(self.symbol); info = mt5.symbol_info(self.symbol)
            if tick and info:
                self.mid = round((tick.bid + tick.ask) / 2, self.digits)
                self.step_pts = self._compute_step_pts(tick, info)
            self._set_status('Signal-only mode (Grid OFF)')
        # Startup Diagnostics
        try:
            # [FIX] Isolation
            raw_poss = self._get_my_positions()
            cnt_raw = len(raw_poss)
            cnt_filtered = self._open_pos_count_side("buy") + self._open_pos_count_side("sell")
            msg_diag = f"[STARTUP] Positions Check (Symbol={self.symbol}): Raw={cnt_raw} / Filtered(Magic)={cnt_filtered}"
            self._log(msg_diag, level=1)
            if not self.headless:
                print(msg_diag)

            # [FIX] 起動時にGrid条件チェック: Grid無効 or NANPIN以外ならGrid削除
            # grid_enable状態に関わらず、条件を満たさないGrid指値は削除
            print("=" * 80)
            print("[DEBUG-STARTUP] Grid cleanup check starting...")
            print(f"[DEBUG-STARTUP] grid_enable = {self.grid_enable}")
            print(f"[DEBUG-STARTUP] _current_mode_str = {self._current_mode_str}")
            should_clear = (not self.grid_enable) or (self._current_mode_str != "NANPIN (Recovery)")
            print(f"[DEBUG-STARTUP] should_clear = {should_clear}")
            print("=" * 80)
            self._log(f"[STARTUP] Grid cleanup check: grid_enable={self.grid_enable}, mode={self._current_mode_str}, should_clear={should_clear}", level=1)

            if should_clear:
                # まず全オーダーを確認
                print("[DEBUG-STARTUP] Executing grid cleanup...")
                all_orders = self._get_my_orders()
                print(f"[DEBUG-STARTUP] Total orders found: {len(all_orders)}")

                # 全オーダーのコメントを表示
                print(f"[DEBUG-STARTUP] All order comments:")
                for i, o in enumerate(all_orders[:10], 1):  # 最初の10件
                    print(f"[DEBUG-STARTUP]   Order {i}: ticket={o.ticket}, type={o.type}, comment='{o.comment}'")

                grid_orders = [o for o in all_orders if "recenter" in (o.comment or "")]
                print(f"[DEBUG-STARTUP] Grid orders found: {len(grid_orders)}")
                print(f"[DEBUG-STARTUP] GRID_TAG = '{GRID_TAG}'")

                self._log(f"[STARTUP] Found {len(all_orders)} total orders, {len(grid_orders)} grid orders (tag='{GRID_TAG}')", level=1)

                print("[DEBUG-STARTUP] Calling _clear_all_grid_orders...")
                cleared, failed, rate_limited = self._clear_all_grid_orders(reason="startup cleanup")
                print(f"[DEBUG-STARTUP] Cleanup result: cleared={cleared}, failed={failed}, rate_limited={rate_limited}")
                reason_detail = "grid disabled" if not self.grid_enable else f"mode={self._current_mode_str}"
                self._log(f"[STARTUP] Grid cleanup result: cleared={cleared}, failed={failed}, rate_limited={rate_limited} ({reason_detail})", level=1)
            else:
                print("[DEBUG-STARTUP] should_clear is False, skipping cleanup")
        except Exception as e:
            self._log(f"[STARTUP] Pos check failed: {e}", level=1)

        # 監視開始
        print("=" * 80)
        print("[DEBUG-STARTUP] Starting monitor thread...")
        print("=" * 80)
        self.running = True
        self._monitor_thread = threading.Thread(target=self._monitor, daemon=True)
        self._monitor_thread.start()
        print("[DEBUG-STARTUP] Monitor thread started, entering main loop...")

        try:
            if self.headless:
                try:
                    while self.running:
                        time.sleep(0.05)
                except KeyboardInterrupt:
                    pass
            else:
                # [FIX] Threading Fix for Unified Mode
                # If we are a child (gui_parent exists), do NOT call mainloop here.
                # Mainloop is handled by the main thread.
                if getattr(self, "gui_parent", None):
                    # Just wait for shutdown signal
                    while self.running:
                        time.sleep(0.1)
                else:
                    self.root.mainloop()
                    self.running = False
                    if self._monitor_thread.is_alive():
                        self._monitor_thread.join(timeout=2.0)
        finally:
            self._save_offset_state_to_disk()
            self._save_pivot_config()
            # ONLY shutdown if we are the only instance or it's a single run
            # In Unified Mode, shutdown is handled by main()
            if not getattr(self, "gui_parent", None):
                try: mt5.shutdown()
                except: pass


# ══════════════════════════ MAIN (GUI) ═════════════════════════
def main() -> None:
    auto_mode = "--auto" in sys.argv
    res = None
    term = None
    conf_path = os.path.join(os.path.dirname(__file__) or ".", "config_auto_usd.json")

    # 1. Load config if auto mode
    if auto_mode:
        if os.path.exists(conf_path):
            try:
                print(f"[AUTO] Loading config from {conf_path}...")
                with open(conf_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                
                # Check format (Dict or List)
                if isinstance(data, dict):
                    term = data.get("terminal")
                    res = data.get("params")
                    if isinstance(res, list): res = tuple(res)
                elif isinstance(data, list):
                    res = tuple(data)
                    # term is None, so choose_terminal will be called fallback? 
                    # Actually we want to automate terminal too, but old config doesn't have it.
                    # User must run manual ONCE to save terminal.
            except Exception as e:
                print(f"[AUTO] Failed to load config: {e}")
                sys.exit(1)
        else:
            print(f"[AUTO] Config file not found: {conf_path}")
            sys.exit(1)

    # 2. Terminal Selection
    if not term:
        term = choose_terminal()
        if not term: sys.exit("No MT5 terminal selected – exiting.")
    
    # 3. Init MT5
    print(f"[INIT] Initializing MT5 with path: {term}")
    if not mt5.initialize(path=term) if term else mt5.initialize():
        print("[INIT] Path-specific init failed, attempting default initialization...")
        if not mt5.initialize():
            c, m = mt5.last_error()
            sys.exit(f"MT5 init failed: {c} {m} (Path: {term})")

    # 4. Params Input
    if res is None:
        root = tk.Tk(); root.withdraw()
        pd = ParamDialog(root); pd.wait_window(); root.destroy()
        mt5.shutdown()
        if pd.res is None: sys.exit("Parameters dialog canceled – exiting.")
        res = pd.res
        
        # 5. Save Config (Manual mode only - update config)
        try:
            with open(conf_path, "w", encoding="utf-8") as f:
                json.dump({
                    "terminal": term,
                    "params": res,
                    "version": 2
                }, f, ensure_ascii=False, indent=2)
            print(f"[CONFIG] Saved auto-start config to {conf_path}")
        except Exception as e:
            print(f"[CONFIG] Failed to save config: {e}")

    else:
        # Auto mode
        pass # Keep MT5 initialized

    # Unpack according to new schema (added: discord_en, discord_url, risk guard settings, profile)
    # Default profile name
    prof_name = "Scalp (M1)"

    pivot_skip_c1 = PIVOT_STRICT_SKIP_C1
    req_ref2_bb = PIVOT_FIRST_REQUIRE_REF2_BODY_BREAK
    req_upper_bb = PIVOT_FIRST_REQUIRE_UPPER_BODY_BREAK
    zigzag_gate = PIVOT_ZIGZAG_ENTRY_ENABLE
    nanpin_hedge = NANPIN_FULL_HEDGE_ENABLE
    nanpin_prevent = NANPIN_PREVENT_ENABLE

    if len(res) == 37:
        sym, digs, lot, nside, spread_mult, pair_th, mae_cd, tf_cd, grid_en, mae_en, tf_en, grid_mode, strict, keepn, flipr, flipcd, min_dist_mult, tfl_live, tfl_mult, mae_live, mae_mult, pivot_en, pivot_cd, pivot_skip_c1, disc_en, disc_url, volatility_en, volatility_mult, block_time_en, block_time_start, block_time_end, prof_name, req_ref2_bb, req_upper_bb, zigzag_gate, nanpin_hedge, nanpin_prevent = (*res,)
    elif len(res) == 36:
        sym, digs, lot, nside, spread_mult, pair_th, mae_cd, tf_cd, grid_en, mae_en, tf_en, grid_mode, strict, keepn, flipr, flipcd, min_dist_mult, tfl_live, tfl_mult, mae_live, mae_mult, pivot_en, pivot_cd, pivot_skip_c1, disc_en, disc_url, volatility_en, volatility_mult, block_time_en, block_time_start, block_time_end, prof_name, req_ref2_bb, req_upper_bb, zigzag_gate, nanpin_hedge = (*res,)
    elif len(res) == 32:
        sym, digs, lot, nside, spread_mult, pair_th, mae_cd, tf_cd, grid_en, mae_en, tf_en, grid_mode, strict, keepn, flipr, flipcd, min_dist_mult, tfl_live, tfl_mult, mae_live, mae_mult, pivot_en, pivot_cd, pivot_skip_c1, disc_en, disc_url, volatility_en, volatility_mult, block_time_en, block_time_start, block_time_end, prof_name = (*res,)
    elif len(res) == 31:
        sym, digs, lot, nside, spread_mult, pair_th, mae_cd, tf_cd, grid_en, mae_en, tf_en, grid_mode, strict, keepn, flipr, flipcd, min_dist_mult, tfl_live, tfl_mult, mae_live, mae_mult, pivot_en, pivot_cd, disc_en, disc_url, volatility_en, volatility_mult, block_time_en, block_time_start, block_time_end, prof_name = (*res,)
    elif len(res) == 30:
        sym, digs, lot, nside, spread_mult, pair_th, mae_cd, tf_cd, grid_en, mae_en, tf_en, grid_mode, strict, keepn, flipr, flipcd, min_dist_mult, tfl_live, tfl_mult, mae_live, mae_mult, pivot_en, pivot_cd, disc_en, disc_url, volatility_en, volatility_mult, block_time_en, block_time_start, block_time_end = (*res,)
    elif len(res) == 25:
        sym, digs, lot, nside, spread_mult, pair_th, mae_cd, tf_cd, grid_en, mae_en, tf_en, grid_mode, strict, keepn, flipr, flipcd, min_dist_mult, tfl_live, tfl_mult, mae_live, mae_mult,pivot_en, pivot_cd, disc_en, disc_url = (*res,)
        volatility_en, volatility_mult, block_time_en, block_time_start, block_time_end = VOLATILITY_GUARD_ENABLE, VOLATILITY_ATR_MULT, BLOCK_TIME_ENABLE, BLOCK_TIME_START_HOUR, BLOCK_TIME_END_HOUR
    elif len(res) == 23:
        # fallback (older dialog): append defaults
        sym, digs, lot, nside, spread_mult, pair_th, mae_cd, tf_cd, grid_en, mae_en, tf_en, grid_mode, strict, keepn, flipr, flipcd, min_dist_mult, tfl_live, tfl_mult, mae_live, mae_mult,pivot_en, pivot_cd = res
        disc_en, disc_url = True, ""
        volatility_en, volatility_mult, block_time_en, block_time_start, block_time_end = VOLATILITY_GUARD_ENABLE, VOLATILITY_ATR_MULT, BLOCK_TIME_ENABLE, BLOCK_TIME_START_HOUR, BLOCK_TIME_END_HOUR
    elif len(res) == 19:
        # fallback (older dialog): append defaults
        sym, digs, lot, nside, spread_mult, pair_th, mae_cd, tf_cd, grid_en, mae_en, tf_en, grid_mode, strict, keepn, flipr, flipcd, min_dist_mult = res
        tfl_live, tfl_mult, mae_live, mae_mult,pivot_en, pivot_cd = TFLOW_MAX_LIVE, TFLOW_LOT_MULT, MAE_MAX_LIVE, MAE_LOT_MULT
        disc_en, disc_url = True, ""
        volatility_en, volatility_mult, block_time_en, block_time_start, block_time_end = VOLATILITY_GUARD_ENABLE, VOLATILITY_ATR_MULT, BLOCK_TIME_ENABLE, BLOCK_TIME_START_HOUR, BLOCK_TIME_END_HOUR
    kwargs = {
        "orders_side": nside,
        "spread_mult": spread_mult,
        "pair_profit_threshold": pair_th,
        "grid_enable": grid_en,
        "mae_enable": mae_en,
        "tflow_enable": tf_en,
        "tflow_cooldown": tf_cd,
        "mae_cooldown": mae_cd,
        "grid_mode": grid_mode,
        "strict_pending_cleanup": strict,
        "keep_nearest_slots": keepn,
        "exposure_flip_ratio": flipr,
        "winside_flip_cooldown_sec": flipcd,
        "min_entry_distance_mult": min_dist_mult,
        "tflow_max_live": tfl_live,
        "tflow_lot_mult": tfl_mult,
        "mae_max_live": mae_live,
        "mae_lot_mult": mae_mult,
        "pivot_enable": pivot_en,
        "pivot_cooldown_sec": pivot_cd,
        "pivot_strict_skip_c1": pivot_skip_c1,
        "pivot_skip_c1_override": None,
        "discord_enable": disc_en,
        "discord_url": disc_url,
        "volatility_guard_enable": volatility_en,
        "volatility_atr_mult": volatility_mult,
        "block_time_enable": block_time_en,
        "block_time_start_hour": block_time_start,
        "block_time_end_hour": block_time_end,
        "pivot_first_require_ref2_body_break": req_ref2_bb,
        "pivot_first_require_upper_body_break": req_upper_bb,
        "pivot_zigzag_entry_enable": zigzag_gate,
        "nanpin_full_hedge_enable": nanpin_hedge,
        "nanpin_prevent_enable": nanpin_prevent,
    }

    # Disk override for sticky profile (v10.4)
    prof_disk = _load_profile_from_disk(sym)
    if prof_disk:
        if prof_disk != prof_name:
            print(f"[INIT] Sticky profile override from disk: {prof_disk} (was {prof_name})")
            prof_name = prof_disk

    # --- Unified Mode / Multi-Instance Support ---
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--profile", type=str, default=None)
    parser.add_argument("--pivot-skip-c1", dest="pivot_skip_c1_arg", action="store_const", const=True, default=None)
    parser.add_argument("--pivot-use-c1", dest="pivot_skip_c1_arg", action="store_const", const=False, default=None)
    args, unknown = parser.parse_known_args()
    
    pivot_skip_c1_override = args.pivot_skip_c1_arg
    if args.profile:
        prof_name = args.profile
        print(f"[ARG] Overriding profile from command line: {prof_name}")
    if pivot_skip_c1_override is not None:
        pivot_skip_c1 = bool(pivot_skip_c1_override)
        mode = "2TF (Skip C1)" if pivot_skip_c1 else "3TF (Full)"
        print(f"[ARG] Overriding pivot strict mode: {mode}")
        kwargs["pivot_skip_c1_override"] = pivot_skip_c1_override
        kwargs["pivot_strict_skip_c1"] = pivot_skip_c1
        kwargs["pivot_skip_c1_override"] = pivot_skip_c1_override

    if prof_name == "Unified (M1+M5+M15)":
        print("[INIT] Starting Unified Mode (M1 + M5 + M15)...")
        root = tk.Tk()
        # Ensure initialized
        if not mt5.initialize(path=term) if term else mt5.initialize():
            mt5.initialize()
            
        acc = mt5.account_info()
        acc_str = f"Account: {acc.login}" if acc else "Account: Offline/Unauthorized"
        root.title(f"{sym} | Unified Mode | {acc_str}")
        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True)

        # Tab 1: M1
        f1 = ttk.Frame(nb); nb.add(f1, text="M1 (Scalp)")
        t1 = StopGridTrader(terminal_path=term, symbol=sym, digits=digs, base_lot=lot, initial_profile_name="Scalp (M1)", magic=MAGIC_NUMBER+1, gui_parent=f1, **kwargs)
        
        # Tab 2: M5
        f2 = ttk.Frame(nb); nb.add(f2, text="M5 (Day)")
        t2 = StopGridTrader(terminal_path=term, symbol=sym, digits=digs, base_lot=lot, initial_profile_name="Day (M5)", magic=MAGIC_NUMBER+5, gui_parent=f2, **kwargs)

        # Tab 3: M15
        f3 = ttk.Frame(nb); nb.add(f3, text="M15 (Swing)")
        t3 = StopGridTrader(terminal_path=term, symbol=sym, digits=digs, base_lot=lot, initial_profile_name="Swing (M15)", magic=MAGIC_NUMBER+15, gui_parent=f3, **kwargs)

        # Tab 0: Dashboard (Added Last, or insert at 0? Let's add at 0 for visibility)
        f_dash = ttk.Frame(nb)
        nb.insert(0, f_dash, text="TOTAL (Dash)")
        dash = UnifiedDashboard(f_dash, [t1, t2, t3], symbol=sym)
        dash.pack(fill="both", expand=True)

        for t in [t1, t2, t3]:
            # Ensure GUI reflects Unified Mode even in sub-tabs
            t.profile_var.set("Unified (M1+M5+M15)")
            threading.Thread(target=t.run, daemon=True).start()
        
        root.mainloop()

    else:
        # Single Mode
        StopGridTrader(
                terminal_path=term,
                symbol=sym,
                digits=digs,
                base_lot=lot,
                initial_profile_name=prof_name,
                magic=MAGIC_NUMBER, # default if single
                **kwargs
        ).run()

if __name__ == "__main__":
    main()
