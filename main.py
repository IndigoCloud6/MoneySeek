import json
import logging
import os
import sqlite3
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from tkinter import messagebox
from tkinter import ttk
from tkinter.font import Font
import mplfinance as mpf
import matplotlib
import akshare as ak
import pandas as pd
from openai import OpenAI
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
import queue
import uuid

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

matplotlib.rcParams['font.family'] = 'Microsoft YaHei'
matplotlib.rcParams['axes.unicode_minus'] = False

# 配置文件路径
CONFIG_FILE = "config.json"

# 默认公告内容
DEFAULT_ANNOUNCEMENTS = [
    "系统公告：所有数据来源于公开市场信息，仅供参考，不构成投资建议。"
]

DEFAULT_API_KEY = "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"  # 可选：不建议在代码中留密钥

# 创建配置文件（如果不存在）
if not os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump({"announcements": DEFAULT_ANNOUNCEMENTS, "api_key": DEFAULT_API_KEY}, f, ensure_ascii=False, indent=4)


# 加载API KEY
def load_api_key():
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
            api_key = config.get("api_key")
            if not api_key or api_key.startswith("sk-xxxx"):
                logging.error("请在config.json中配置有效的api_key")
            return api_key
    except Exception as e:
        logging.error(f"读取API Key失败: {e}")
        raise


api_key = load_api_key()
client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")


# Function to save results to Excel
def save_to_excel(results: list, filename: str = "stock_data.xlsx"):
    df = pd.DataFrame(results)
    df.to_excel(filename, index=False, engine='openpyxl')
    print(f"Data saved to {filename}")


def get_stock_info(stock_code):
    if not isinstance(stock_code, str) or not stock_code.isdigit():
        return ('unknown', '非数字代码')
    code = stock_code.zfill(6) if len(stock_code) < 7 else stock_code
    prefix2 = code[:2]
    prefix3 = code[:3]
    if prefix3 == '920':
        return ('bj', '北交所')
    if prefix3 in ('600', '601', '603', '605'):
        return ('sh', '沪市主板')
    elif prefix3 == '688':
        return ('sh', '科创板')
    elif prefix3 in ('000', '001', '002', '003', '004'):
        return ('sz', '深市主板')
    elif prefix3 in ('300', '301'):
        return ('sz', '创业板')
    elif prefix2 == '20':
        return ('sz', '深市B股')
    elif prefix3 == '900':
        return ('sh', '沪市B股')
    elif prefix3 in ('430', '831', '832', '833', '834', '835', '836', '837', '838', '839'):
        return ('bj', '北交所')
    elif prefix3 in ('400', '430', '830'):
        return ('bj', '北交所')
    elif prefix2 == '87':
        return ('bj', '北交所')
    elif prefix2 == '83':
        return ('bj', '北交所')
    elif code[0] == '8' and prefix3 != '920':
        return ('bj', '北交所')
    else:
        return ('unknown', '其他板块')


class KLineWindow:
    """独立的K线图窗口类"""

    def __init__(self, parent, stock_code, stock_name):
        self.parent = parent
        self.stock_code = stock_code
        self.stock_name = stock_name
        self.window = None
        self.canvas = None
        self.loading_label = None
        self.result_queue = queue.Queue()
        self.window_id = str(uuid.uuid4())[:8]  # 生成唯一窗口ID

        # 创建窗口
        self.create_window()

        # 在后台获取数据
        threading.Thread(target=self.fetch_data_async, daemon=True).start()

        # 定期检查结果
        self.check_result()

    def create_window(self):
        """创建K线图窗口"""
        self.window = tk.Toplevel(self.parent)
        self.window.title(f"K线图 - {self.stock_name}({self.stock_code}) [ID: {self.window_id}]")
        self.window.geometry("1200x800")

        # 居中显示
        self.center_window()

        # 创建主框架
        main_frame = ttk.Frame(self.window)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # 创建状态框架
        status_frame = ttk.Frame(main_frame)
        status_frame.pack(fill=tk.X, pady=(0, 10))

        # 加载提示
        self.loading_label = ttk.Label(
            status_frame,
            text=f"正在加载 {self.stock_name}({self.stock_code}) 的K线数据...",
            font=('Microsoft YaHei', 12)
        )
        self.loading_label.pack(side=tk.LEFT)

        # 进度指示器
        self.progress = ttk.Progressbar(status_frame, mode='indeterminate')
        self.progress.pack(side=tk.RIGHT, padx=(10, 0))
        self.progress.start()

        # 图表容器
        self.chart_frame = ttk.Frame(main_frame)
        self.chart_frame.pack(fill=tk.BOTH, expand=True)

        # 窗口关闭事件
        self.window.protocol("WM_DELETE_WINDOW", self.on_window_close)

    def center_window(self):
        """窗口居中"""
        self.window.update_idletasks()
        width = 1200
        height = 800
        x = (self.window.winfo_screenwidth() // 2) - (width // 2)
        y = (self.window.winfo_screenheight() // 2) - (height // 2)
        self.window.geometry(f'{width}x{height}+{x}+{y}')

    def fetch_data_async(self):
        """异步获取K线数据"""
        try:
            from datetime import datetime, timedelta

            # 获取交易日期逻辑
            now = datetime.now()
            current_time = now.time()
            market_open_time = datetime.strptime("09:30", "%H:%M").time()

            # 如果当前时间早于9:30，使用前一天的日期
            if current_time < market_open_time:
                target_date = now - timedelta(days=1)
            else:
                target_date = now

            # 进一步处理周末情况
            while target_date.weekday() > 4:  # 0-6代表周一到周日
                target_date = target_date - timedelta(days=1)

            today = target_date.strftime('%Y%m%d')

            logging.info(f"[{self.window_id}] 开始获取 {self.stock_name}({self.stock_code}) 的K线数据，日期: {today}")

            # 获取股票1分钟K线数据
            stock_data = ak.stock_zh_a_hist_min_em(
                symbol=self.stock_code,
                period="1",
                start_date=f"{today} 09:00:00",
                end_date=f"{today} 15:00:00",
                adjust="qfq"
            )

            if stock_data.empty:
                self.result_queue.put({
                    'success': False,
                    'error': f"未获取到{self.stock_name}({self.stock_code})的数据，可能是非交易日或数据源问题"
                })
                return

            # 数据预处理
            stock_data_processed = stock_data.rename(columns={
                '时间': 'Date',
                '开盘': 'Open',
                '最高': 'High',
                '最低': 'Low',
                '收盘': 'Close',
                '成交量': 'Volume'
            })

            # 转换时间格式并设置为索引
            stock_data_processed['Date'] = pd.to_datetime(stock_data_processed['Date'])
            stock_data_processed.set_index('Date', inplace=True)

            # 确保数据类型正确
            for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
                stock_data_processed[col] = pd.to_numeric(stock_data_processed[col], errors='coerce')

            # 计算技术指标
            # 移动平均线
            stock_data_processed['MA5'] = stock_data_processed['Close'].rolling(window=5).mean()
            stock_data_processed['MA10'] = stock_data_processed['Close'].rolling(window=10).mean()
            stock_data_processed['MA20'] = stock_data_processed['Close'].rolling(window=20).mean()

            # 布林带
            stock_data_processed['BB_middle'] = stock_data_processed['Close'].rolling(window=20).mean()
            stock_data_processed['BB_std'] = stock_data_processed['Close'].rolling(window=20).std()
            stock_data_processed['BB_upper'] = stock_data_processed['BB_middle'] + 2 * stock_data_processed['BB_std']
            stock_data_processed['BB_lower'] = stock_data_processed['BB_middle'] - 2 * stock_data_processed['BB_std']

            # RSI 相对强弱指标
            def calculate_rsi(data, window=14):
                delta = data.diff()
                gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
                loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
                rs = gain / loss
                rsi = 100 - (100 / (1 + rs))
                return rsi

            stock_data_processed['RSI'] = calculate_rsi(stock_data_processed['Close'])

            # 将处理好的数据放入队列
            self.result_queue.put({
                'success': True,
                'data': stock_data_processed,
                'display_date': target_date.strftime('%Y-%m-%d')
            })

            logging.info(f"[{self.window_id}] {self.stock_name}({self.stock_code}) 数据获取完成")

        except Exception as e:
            logging.error(f"[{self.window_id}] 获取K线数据失败: {e}")
            self.result_queue.put({
                'success': False,
                'error': f"获取K线数据失败: {str(e)}"
            })

    def check_result(self):
        """检查数据获取结果"""
        try:
            result = self.result_queue.get_nowait()
            if result['success']:
                self.display_chart(result['data'], result['display_date'])
            else:
                self.show_error(result['error'])
        except queue.Empty:
            # 如果窗口还存在，继续检查
            if self.window and self.window.winfo_exists():
                self.window.after(100, self.check_result)

    def display_chart(self, stock_data_processed, display_date):
        """显示K线图"""
        try:
            # 停止进度条
            self.progress.stop()
            self.loading_label.config(text=f"正在绘制 {self.stock_name}({self.stock_code}) 的K线图...")

            # 创建自定义颜色样式（中国习惯：红涨绿跌）
            mc = mpf.make_marketcolors(
                up='red',
                down='green',
                edge='inherit',
                wick={'up': 'red', 'down': 'green'},
                volume='in',
            )

            # 创建图表样式
            style = mpf.make_mpf_style(
                marketcolors=mc,
                gridstyle='-',
                gridcolor='lightgray',
                facecolor='white',
                figcolor='white',
                rc={'font.family': 'Microsoft YaHei'}  # 支持中文显示
            )

            # 准备附加图表（技术指标）
            apds = [
                # 移动平均线
                mpf.make_addplot(stock_data_processed['MA5'], color='blue', width=1.5),
                mpf.make_addplot(stock_data_processed['MA10'], color='purple', width=1.5),
                mpf.make_addplot(stock_data_processed['MA20'], color='orange', width=1.5),

                # 布林带
                mpf.make_addplot(stock_data_processed['BB_upper'], color='gray', width=1, alpha=0.7),
                mpf.make_addplot(stock_data_processed['BB_lower'], color='gray', width=1, alpha=0.7),

                # RSI (在第3个子图中显示)
                mpf.make_addplot(stock_data_processed['RSI'], panel=2, color='purple', width=1.5),
                mpf.make_addplot([70] * len(stock_data_processed), panel=2, color='red', width=0.8, linestyle='--', alpha=0.7),
                mpf.make_addplot([30] * len(stock_data_processed), panel=2, color='green', width=0.8, linestyle='--', alpha=0.7),
            ]

            # 创建matplotlib图形 - 关键：不使用plt.show()
            fig, axes = mpf.plot(
                stock_data_processed,
                type='candle',
                style=style,
                volume=True,
                addplot=apds,
                ylabel='价格 (元)',
                ylabel_lower='成交量',
                figsize=(12, 8),
                panel_ratios=(3, 1, 1),
                tight_layout=True,
                show_nontrading=False,
                returnfig=True  # 关键：返回图形对象而不是显示
            )

            # 添加图例
            main_ax = axes[0]
            legend_elements = [
                plt.Line2D([0], [0], color='blue', lw=1.5, label='MA5'),
                plt.Line2D([0], [0], color='purple', lw=1.5, label='MA10'),
                plt.Line2D([0], [0], color='orange', lw=1.5, label='MA20'),
                plt.Line2D([0], [0], color='gray', lw=1, alpha=0.7, label='布林带'),
            ]
            main_ax.legend(handles=legend_elements, loc='lower right', frameon=True,
                           fancybox=True, shadow=True, framealpha=0.9, fontsize=10)

            # 为RSI子图添加图例
            if len(axes) > 2:
                rsi_ax = axes[2]
                rsi_legend_elements = [
                    plt.Line2D([0], [0], color='purple', lw=1.5, label='RSI'),
                    plt.Line2D([0], [0], color='red', lw=0.8, linestyle='--', alpha=0.7, label='超买(70)'),
                    plt.Line2D([0], [0], color='green', lw=0.8, linestyle='--', alpha=0.7, label='超卖(30)'),
                ]
                rsi_ax.legend(handles=rsi_legend_elements, loc='lower right', frameon=True,
                              fancybox=True, shadow=True, framealpha=0.9, fontsize=9)

            # 设置标题
            fig.suptitle(f'{self.stock_name}({self.stock_code}) - {display_date} 高级技术分析K线图',
                         fontsize=14, fontweight='bold')

            # 清空图表容器
            for widget in self.chart_frame.winfo_children():
                widget.destroy()

            # 在Tkinter中嵌入matplotlib图形
            self.canvas = FigureCanvasTkAgg(fig, self.chart_frame)
            self.canvas.draw()
            self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

            # 添加工具栏
            toolbar = NavigationToolbar2Tk(self.canvas, self.chart_frame)
            toolbar.update()

            # 隐藏加载提示
            self.loading_label.config(text=f"{self.stock_name}({self.stock_code}) K线图加载完成")

            # 打印技术指标
            if not stock_data_processed.empty:
                latest_data = stock_data_processed.iloc[-1]
                logging.info(f"[{self.window_id}] {self.stock_name}({self.stock_code}) 最新数据:")
                logging.info(f"收盘价: {latest_data['Close']:.2f}, MA5: {latest_data['MA5']:.2f}, RSI: {latest_data['RSI']:.2f}")

        except Exception as e:
            logging.error(f"[{self.window_id}] 显示K线图失败: {e}")
            self.show_error(f"显示K线图失败: {str(e)}")

    def show_error(self, error_message):
        """显示错误信息"""
        self.progress.stop()
        self.loading_label.config(text="加载失败")

        error_frame = ttk.Frame(self.chart_frame)
        error_frame.pack(expand=True)

        ttk.Label(error_frame, text="❌", font=('Arial', 48)).pack(pady=20)
        ttk.Label(error_frame, text=error_message, font=('Microsoft YaHei', 12),
                  foreground='red', wraplength=800).pack(pady=10)

        ttk.Button(error_frame, text="重试",
                   command=lambda: self.retry_fetch()).pack(pady=10)

    def retry_fetch(self):
        """重试获取数据"""
        # 清空图表容器
        for widget in self.chart_frame.winfo_children():
            widget.destroy()

        # 重新显示加载状态
        self.loading_label.config(text=f"正在重新加载 {self.stock_name}({self.stock_code}) 的K线数据...")
        self.progress.start()

        # 重新获取数据
        threading.Thread(target=self.fetch_data_async, daemon=True).start()
        self.check_result()

    def on_window_close(self):
        """窗口关闭处理"""
        logging.info(f"[{self.window_id}] 关闭K线图窗口: {self.stock_name}({self.stock_code})")
        if self.canvas:
            self.canvas.get_tk_widget().destroy()
        self.window.destroy()


class StockVisualizationApp:
    def __init__(self, master):
        self.master = master
        master.title("草船借箭")
        self.center_window(master, 1200, 650)

        self.announcements = self.load_announcements()
        self.current_announcement_idx = 0
        self.display_columns = ["代码", "名称", "交易所", "行业", "总市值", "最新", "涨幅", "今开", "最高", "最低", "总成交金额"]

        self.bold_font = Font(weight="bold")
        self.normal_font = Font(weight="normal")
        self.announcement_font = Font(family="Microsoft YaHei", size=10, weight="bold")

        self.main_frame = ttk.Frame(master)
        self.main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # K线图窗口管理
        self.kline_windows = {}  # 存储所有打开的K线图窗口
        self.kline_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="KLine")  # 限制并发数

        self.create_announcement_bar()
        self.status_label = ttk.Label(self.main_frame, text="")
        self.status_label.pack(side=tk.BOTTOM, fill=tk.X)
        self.create_control_panel()
        self.create_data_table()
        threading.Thread(target=self.fetch_data, daemon=True).start()
        self.selected_stock = {"code": "", "name": ""}
        self.update_announcement()
        self.update_clock()

    def center_window(self, window, width, height):
        window.withdraw()
        window.update_idletasks()
        screenwidth = window.winfo_screenwidth()
        screenheight = window.winfo_screenheight()
        x = int((screenwidth - width) / 2)
        y = int((screenheight - height) / 2)
        window.geometry(f"{width}x{height}+{x}+{y}")
        window.deiconify()

    def load_announcements(self):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                announcements = config.get("announcements", DEFAULT_ANNOUNCEMENTS)
                if not announcements:
                    return DEFAULT_ANNOUNCEMENTS
                return announcements
        except Exception as e:
            logging.error(f"加载公告配置文件失败: {e}")
            return DEFAULT_ANNOUNCEMENTS

    def create_announcement_bar(self):
        announcement_frame = ttk.Frame(self.main_frame, height=30)
        announcement_frame.pack(fill=tk.X, padx=5, pady=(0, 5))
        self.announcement_icon = tk.Label(
            announcement_frame,
            text="📢",
            font=self.announcement_font,
            bg="#FFE4B5",
            padx=5
        )
        self.announcement_icon.pack(side=tk.LEFT, fill=tk.Y)
        self.announcement_label = tk.Label(
            announcement_frame,
            text="",
            font=self.announcement_font,
            bg="#FFE4B5",
            fg="#8B0000",
            anchor="w",
            padx=10
        )
        self.announcement_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.clock_label = tk.Label(
            announcement_frame,
            text="",
            font=("Microsoft YaHei", 10, "bold"),
            bg="#FFE4B5",
            fg="#FF0000",
            padx=10
        )
        self.clock_label.pack(side=tk.RIGHT, padx=5)
        ttk.Button(
            announcement_frame,
            text="配置公告",
            width=10,
            command=self.configure_announcements
        ).pack(side=tk.RIGHT, padx=5)
        announcement_frame.configure(style="Announcement.TFrame")
        style = ttk.Style()
        style.configure("Announcement.TFrame", background="#FFE4B5")

    def update_clock(self):
        now = datetime.now()
        time_str = now.strftime("%Y-%m-%d %H:%M:%S")
        self.clock_label.config(text=time_str)
        self.master.after(1000, self.update_clock)

    def update_announcement(self):
        if self.announcements:
            announcement = self.announcements[self.current_announcement_idx]
            self.announcement_label.config(text=announcement)
            self.current_announcement_idx = (self.current_announcement_idx + 1) % len(self.announcements)
            self.master.after(8000, self.update_announcement)

    def configure_announcements(self):
        config_window = tk.Toplevel(self.master)
        config_window.title("公告配置")
        self.center_window(config_window, 600, 800)
        config_window.resizable(True, True)

        # 外层frame，确保按钮不会被内容挤出窗口
        outer_frame = ttk.Frame(config_window)
        outer_frame.pack(fill=tk.BOTH, expand=True)

        # 编辑区
        text_frame = ttk.Frame(outer_frame)
        text_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 0))

        scrollbar = ttk.Scrollbar(text_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.announcement_text = tk.Text(
            text_frame,
            wrap=tk.WORD,
            yscrollcommand=scrollbar.set,
            font=("Microsoft YaHei", 10),
            padx=10,
            pady=10
        )
        self.announcement_text.pack(fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.announcement_text.yview)

        # 帮助说明
        help_text = "提示：每条公告单独一行，系统将按顺序轮播显示"
        ttk.Label(outer_frame, text=help_text, foreground="gray").pack(anchor=tk.W, padx=10, pady=(6, 0))

        # 按钮区
        button_frame = ttk.Frame(outer_frame)
        button_frame.pack(fill=tk.X, padx=10, pady=10, side=tk.BOTTOM)

        ttk.Button(
            button_frame,
            text="取消",
            command=config_window.destroy
        ).pack(side=tk.RIGHT, padx=5)
        ttk.Button(
            button_frame,
            text="重置",
            command=self.reset_announcements
        ).pack(side=tk.RIGHT, padx=5)
        ttk.Button(
            button_frame,
            text="保存",
            command=lambda: self.save_announcements(config_window)
        ).pack(side=tk.RIGHT, padx=5)

        self.load_announcements_to_text()

    def show_ai_diagnose(self):
        if not self.selected_stock["code"]:
            messagebox.showwarning("提示", "请先选择一只股票")
            return

        stock_code = self.selected_stock["code"]
        stock_name = self.selected_stock["name"]

        dialog = tk.Toplevel(self.master)
        dialog.title(f"AI诊股: {stock_name}({stock_code})")
        self.center_window(dialog, 600, 400)
        text_widget = tk.Text(dialog, wrap=tk.WORD, state=tk.NORMAL)
        text_widget.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        font_bold_large = ("Microsoft YaHei", 14, "bold")
        text_widget.configure(font=font_bold_large)
        text_widget.insert(tk.END, "正在咨询AI诊股，请稍候...\n")
        text_widget.config(state=tk.DISABLED)

        def stream_gpt_response():
            prompt = f"请用中文分析股票 {stock_name}({stock_code}) 的投资价值、风险、行业地位和未来走势。"
            try:
                stream = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[{"role": "user", "content": prompt}],
                    stream=True,
                    extra_body={
                        "web_search": True  # 启用联网功能
                    }
                )
                text_widget.config(state=tk.NORMAL)
                text_widget.delete(1.0, tk.END)

                for chunk in stream:
                    if chunk.choices[0].delta.content:
                        content = chunk.choices[0].delta.content
                        text_widget.insert(tk.END, content)
                        text_widget.see(tk.END)
                        text_widget.update()
                text_widget.config(state=tk.DISABLED)
            except Exception as e:
                text_widget.config(state=tk.NORMAL)
                text_widget.insert(tk.END, f"\n[AI诊股失败]: {e}")
                text_widget.config(state=tk.DISABLED)

        threading.Thread(target=stream_gpt_response, daemon=True).start()

    def load_announcements_to_text(self):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                announcements = config.get("announcements", DEFAULT_ANNOUNCEMENTS)
                text = "\n".join(announcements)
                self.announcement_text.delete(1.0, tk.END)
                self.announcement_text.insert(tk.END, text)
        except Exception as e:
            logging.error(f"加载公告到文本框失败: {e}")
            self.announcement_text.insert(tk.END, "\n".join(DEFAULT_ANNOUNCEMENTS))

    def save_announcements(self, window):
        text = self.announcement_text.get(1.0, tk.END).strip()
        announcements = [line.strip() for line in text.split("\n") if line.strip()]
        if not announcements:
            messagebox.showerror("错误", "公告内容不能为空！")
            return
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump({"announcements": announcements}, f, ensure_ascii=False, indent=4)
            self.announcements = announcements
            self.current_announcement_idx = 0
            self.update_announcement()
            messagebox.showinfo("成功", "公告配置已保存！")
            window.destroy()
        except Exception as e:
            logging.error(f"保存公告配置失败: {e}")
            messagebox.showerror("错误", f"保存公告配置失败: {str(e)}")

    def reset_announcements(self):
        self.announcement_text.delete(1.0, tk.END)
        self.announcement_text.insert(tk.END, "\n".join(DEFAULT_ANNOUNCEMENTS))

    def fetch_data(self):
        try:
            self.status_label.config(text="正在获取数据...")
            self.master.update()
            stock_changes_em_df = ak.stock_changes_em(symbol="大笔买入")
            split_info = stock_changes_em_df['相关信息'].str.split(',', expand=True)
            split_info.columns = ['成交量', '成交价', '占成交量比', '成交金额']
            split_info['成交量'] = pd.to_numeric(split_info['成交量'], errors='coerce')
            split_info['成交价'] = pd.to_numeric(split_info['成交价'], errors='coerce')
            split_info['占成交量比'] = pd.to_numeric(split_info['占成交量比'], errors='coerce')
            split_info['成交金额'] = pd.to_numeric(split_info['成交金额'], errors='coerce')
            stock_changes_em_df = pd.concat([stock_changes_em_df.drop(columns=['相关信息']), split_info], axis=1)
            current_date = datetime.now().strftime('%Y%m%d')
            current_date_obj = datetime.now().date()
            stock_changes_em_df['时间'] = pd.to_datetime(
                current_date_obj.strftime('%Y-%m-%d') + ' ' + stock_changes_em_df['时间'].apply(lambda x: x.strftime('%H:%M:%S')),
                format='%Y-%m-%d %H:%M:%S'
            )
            conn = sqlite3.connect('stock_data.db')
            table_name = f'stock_changes_{current_date}'
            try:
                conn.execute(f"DELETE FROM {table_name}")
            except sqlite3.OperationalError as e:
                if "no such table" in str(e):
                    pass
                else:
                    raise
            except Exception as e:
                pass
            stock_changes_em_df.to_sql(table_name, conn, if_exists='append', index=False)
            logging.info(f"数据已成功存入 SQLite 数据库表 {table_name}！")
            real_data_list = []
            stock_info = stock_changes_em_df[['代码', '名称']].drop_duplicates(subset=['代码'])

            def not_bj_kcb(row):
                exchange, market = get_stock_info(row['代码'])
                return not (exchange == 'bj' or market == '科创板' or market == '创业板')

            filtered_stock_info = stock_info[stock_info.apply(not_bj_kcb, axis=1)]
            max_workers = min(10, len(filtered_stock_info))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_stock = {
                    executor.submit(self.process_stock, row['代码'], row['名称']): row['代码']
                    for _, row in filtered_stock_info.iterrows()
                }
                for future in as_completed(future_to_stock):
                    result = future.result()
                    if result:
                        real_data_list.append(result)
            stock_real_data_df = pd.DataFrame(real_data_list)
            real_table_name = f'stock_real_data_{current_date}'
            try:
                conn.execute(f"DELETE FROM {real_table_name}")
            except sqlite3.OperationalError as e:
                if "no such table" in str(e):
                    pass
                else:
                    raise
            except Exception as e:
                pass
            stock_real_data_df.to_sql(real_table_name, conn, if_exists='replace', index=False)
            logging.info(f"实时数据已成功存入 SQLite 数据库表 {real_table_name}！")
            conn.close()
            self.status_label.config(text="数据获取完成")
            self.load_data()
        except Exception as e:
            logging.error(f"数据获取失败: {e}")
            self.status_label.config(text="数据获取失败")

    def process_stock(self, stock_code, stock_name):
        try:
            stock_info_df = ak.stock_individual_info_em(symbol=stock_code)
            industry = stock_info_df[stock_info_df['item'] == '行业']['value'].iloc[0] if '行业' in stock_info_df['item'].values else '未知'
            market_cap = stock_info_df[stock_info_df['item'] == '总市值']['value'].iloc[0] if '总市值' in stock_info_df['item'].values else '未知'
            stock_bid_ask_df = ak.stock_bid_ask_em(symbol=stock_code)
            latest_price = float(stock_bid_ask_df[stock_bid_ask_df['item'] == '最新']['value'].iloc[0]) if '最新' in stock_bid_ask_df['item'].values else None
            price_change_percent = float(stock_bid_ask_df[stock_bid_ask_df['item'] == '涨幅']['value'].iloc[0]) if '涨幅' in stock_bid_ask_df[
                'item'].values else None
            opening_price = float(stock_bid_ask_df[stock_bid_ask_df['item'] == '今开']['value'].iloc[0]) if '今开' in stock_bid_ask_df['item'].values else None
            max_price = float(stock_bid_ask_df[stock_bid_ask_df['item'] == '最高']['value'].iloc[0]) if '最高' in stock_bid_ask_df['item'].values else None
            min_price = float(stock_bid_ask_df[stock_bid_ask_df['item'] == '最低']['value'].iloc[0]) if '最低' in stock_bid_ask_df['item'].values else None
            zhang_ting = float(stock_bid_ask_df[stock_bid_ask_df['item'] == '涨停']['value'].iloc[0]) if '涨停' in stock_bid_ask_df['item'].values else None
            exchange, market = get_stock_info(stock_code)
            return {
                '代码': stock_code,
                '名称': stock_name,
                '交易所': exchange,
                '市场板块': market,
                '行业': industry,
                '总市值': int(market_cap / 100000000),
                '最新': latest_price,
                '涨幅': price_change_percent,
                '最高': max_price,
                '最低': min_price,
                '涨停': zhang_ting,
                '今开': opening_price
            }
        except Exception as e:
            logging.error(f"处理股票代码 {stock_code} ({stock_name}) 时出错: {e}")
            return None

    def create_control_panel(self):
        control_frame = ttk.LabelFrame(self.main_frame, text="控制面板", padding=10)
        control_frame.pack(fill=tk.X, padx=5, pady=5)
        ttk.Button(control_frame, text="刷新数据", command=self.fetch_data).pack(side=tk.LEFT, padx=5)
        amount_frame = ttk.Frame(control_frame)
        amount_frame.pack(side=tk.LEFT, padx=5)
        ttk.Label(amount_frame, text="最小成交金额(万):").pack(side=tk.LEFT, padx=5)
        ttk.Button(
            amount_frame,
            text="-",
            width=3,
            command=lambda: self.adjust_amount(-200)
        ).pack(side=tk.LEFT, padx=2)
        self.amount_var = tk.StringVar(value="2000")
        self.amount_label = ttk.Label(
            amount_frame,
            textvariable=self.amount_var,
            width=6,
            anchor="center",
            background="white",
            relief="sunken",
            padding=3
        )
        self.amount_label.pack(side=tk.LEFT, padx=2)
        ttk.Button(
            amount_frame,
            text="+",
            width=3,
            command=lambda: self.adjust_amount(200)
        ).pack(side=tk.LEFT, padx=2)

        # 总市值过滤
        market_cap_frame = ttk.Frame(control_frame)
        market_cap_frame.pack(side=tk.LEFT, padx=5)
        ttk.Label(market_cap_frame, text="最小总市值(亿):").pack(side=tk.LEFT, padx=5)
        ttk.Button(
            market_cap_frame,
            text="-",
            width=3,
            command=lambda: self.adjust_market_cap(-10)
        ).pack(side=tk.LEFT, padx=2)
        self.market_cap_var = tk.StringVar(value="10")
        self.market_cap_label = ttk.Label(
            market_cap_frame,
            textvariable=self.market_cap_var,
            width=6,
            anchor="center",
            background="white",
            relief="sunken",
            padding=3
        )
        self.market_cap_label.pack(side=tk.LEFT, padx=2)
        ttk.Button(
            market_cap_frame,
            text="+",
            width=3,
            command=lambda: self.adjust_market_cap(10)
        ).pack(side=tk.LEFT, padx=2)

        ttk.Label(control_frame, text="排序方式:").pack(side=tk.LEFT, padx=5)
        self.sort_var = tk.StringVar(value="总成交金额")
        sort_options = ["总成交金额", "涨幅", "总成笔数"]
        sort_combo = ttk.Combobox(control_frame, textvariable=self.sort_var, values=sort_options, width=10, state="readonly")
        sort_combo.pack(side=tk.LEFT, padx=5)
        sort_combo.bind("<<ComboboxSelected>>", lambda e: self.load_data())
        ttk.Button(control_frame, text="选择显示字段", command=self.select_columns).pack(side=tk.RIGHT, padx=5)

    def adjust_amount(self, delta):
        try:
            current = int(self.amount_var.get())
            new_value = max(0, current + delta)
            self.amount_var.set(str(new_value))
            self.load_data()
        except ValueError:
            self.amount_var.set("2000")
            self.load_data()

    def adjust_market_cap(self, delta):
        try:
            current = int(self.market_cap_var.get())
            new_value = max(0, current + delta)
            self.market_cap_var.set(str(new_value))
            self.load_data()
        except ValueError:
            self.market_cap_var.set("100")
            self.load_data()

    def select_columns(self):
        select_window = tk.Toplevel(self.master)
        select_window.title("选择显示字段")
        self.center_window(select_window, 300, 600)
        all_columns = [
            "代码", "名称", "交易所", "市场板块", "总市值",
            "今开", "涨幅", "最新", "最低", "最高", "涨停",
            "总成笔数", "总成交金额", "时间金额明细"
        ]
        self.column_vars = {}
        for col in all_columns:
            var = tk.BooleanVar(value=col in self.display_columns)
            self.column_vars[col] = var
            cb = ttk.Checkbutton(select_window, text=col, variable=var)
            cb.pack(anchor=tk.W, padx=10, pady=2)
        ttk.Button(
            select_window,
            text="确认",
            command=lambda: self.apply_column_selection(select_window)
        ).pack(side=tk.BOTTOM, pady=10)

    def apply_column_selection(self, window):
        self.display_columns = [col for col, var in self.column_vars.items() if var.get()]
        window.destroy()
        self.load_data()

    def create_data_table(self):
        self.table_frame = ttk.Frame(self.main_frame)
        self.table_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        ttk.Label(self.table_frame, text="股票交易明细", font=('Microsoft YaHei', 12, 'bold')).pack(anchor=tk.W)
        self.tree_container = ttk.Frame(self.table_frame)
        self.tree_container.pack(fill=tk.BOTH, expand=True)

        # 创建loading覆盖层
        self.loading_frame = tk.Frame(self.tree_container, bg='white', bd=2, relief='solid')

        # Loading 文字和图标
        loading_content = tk.Frame(self.loading_frame, bg='white')
        loading_content.pack(expand=True)

        # 加载图标（使用简单的文字符号表示）
        self.loading_icon = tk.Label(loading_content, text="⟳", font=('Arial', 24), bg='white', fg='#2E86AB')
        self.loading_icon.pack(pady=5)

        self.loading_text = tk.Label(loading_content, text="正在加载数据...",
                                     font=('Microsoft YaHei', 12), bg='white', fg='#333333')
        self.loading_text.pack(pady=5)

        self.tree = ttk.Treeview(self.tree_container, show="headings")
        self.vsb = ttk.Scrollbar(self.tree_container, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=self.vsb.set)
        self.hsb = ttk.Scrollbar(self.tree_container, orient="horizontal", command=self.tree.xview)
        self.tree.configure(xscrollcommand=self.hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.hsb.grid(row=1, column=0, sticky="ew")
        self.tree_container.grid_rowconfigure(0, weight=1)
        self.tree_container.grid_columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", self.show_detail)
        self.tree.bind("<Button-3>", self.on_right_click)
        self.context_menu = tk.Menu(self.master, tearoff=0)
        self.context_menu.add_command(label="基本面分析", command=self.show_fundamental)
        self.context_menu.add_command(label="K线图", command=self.show_k_line)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="复制股票代码", command=self.copy_stock_code)
        self.context_menu.add_command(label="复制股票名称", command=self.copy_stock_name)
        self.context_menu.add_command(label="AI诊股", command=self.show_ai_diagnose)

        # 初始化动画相关变量
        self.animation_angle = 0
        self.loading_animation_id = None

    def show_loading(self):
        """显示加载动画"""
        # 显示loading覆盖层
        self.loading_frame.place(x=0, y=0, relwidth=1, relheight=1)
        self.loading_frame.lift()  # 确保loading层在最上方

        # 开始旋转动画
        self.start_loading_animation()

    def hide_loading(self):
        """隐藏加载动画"""
        # 隐藏loading覆盖层
        self.loading_frame.place_forget()

        # 停止旋转动画
        self.stop_loading_animation()

    def start_loading_animation(self):
        """开始loading图标旋转动画"""

        def animate():
            # 根据角度旋转图标（这里用不同的Unicode旋转符号模拟）
            rotation_chars = ["⟳", "⟲", "◐", "◑", "◒", "◓"]
            char_index = (self.animation_angle // 60) % len(rotation_chars)
            self.loading_icon.config(text=rotation_chars[char_index])

            self.animation_angle = (self.animation_angle + 30) % 360
            self.loading_animation_id = self.master.after(100, animate)

        animate()

    def stop_loading_animation(self):
        """停止loading动画"""
        if self.loading_animation_id:
            self.master.after_cancel(self.loading_animation_id)
            self.loading_animation_id = None

    def on_right_click(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            columns = self.tree["columns"]
            values = self.tree.item(item, "values")
            code_idx = columns.index("代码")
            name_idx = columns.index("名称")
            self.selected_stock = {
                "code": values[code_idx],
                "name": values[name_idx]
            }
            self.context_menu.post(event.x_root, event.y_root)

    def show_fundamental(self):
        if self.selected_stock["code"]:
            messagebox.showinfo(
                "基本面分析",
                f"正在获取 {self.selected_stock['name']}({self.selected_stock['code']}) 的基本面数据...\n\n"
                "功能实现中，这里可以展示:\n"
                "- 财务指标(PE, PB, ROE等)\n"
                "- 公司简介\n"
                "- 行业对比\n"
                "- 机构评级\n"
            )

    def show_k_line(self):
        """显示K线图 - 新的并发实现"""
        if not self.selected_stock["code"]:
            messagebox.showwarning("提示", "请先选择一只股票")
            return

        stock_code = self.selected_stock["code"]
        stock_name = self.selected_stock["name"]

        # 检查是否已经有相同股票的K线图窗口打开
        window_key = f"{stock_code}_{stock_name}"
        if window_key in self.kline_windows:
            existing_window = self.kline_windows[window_key]
            if existing_window.window and existing_window.window.winfo_exists():
                # 如果窗口还存在，激活它
                existing_window.window.lift()
                existing_window.window.focus()
                return
            else:
                # 如果窗口已经被关闭，从字典中删除
                del self.kline_windows[window_key]

        # 创建新的K线图窗口
        try:
            kline_window = KLineWindow(self.master, stock_code, stock_name)
            self.kline_windows[window_key] = kline_window

            logging.info(f"创建K线图窗口: {stock_name}({stock_code}), 当前活跃窗口数: {len(self.kline_windows)}")

            # 更新状态栏
            self.status_label.config(text=f"已打开 {stock_name}({stock_code}) 的K线图")

        except Exception as e:
            logging.error(f"创建K线图窗口失败: {e}")
            messagebox.showerror("错误", f"创建K线图窗口失败: {str(e)}")

    def cleanup_closed_windows(self):
        """清理已关闭的K线图窗口"""
        closed_windows = []
        for key, window in self.kline_windows.items():
            if not window.window or not window.window.winfo_exists():
                closed_windows.append(key)

        for key in closed_windows:
            del self.kline_windows[key]

        if closed_windows:
            logging.info(f"清理了 {len(closed_windows)} 个已关闭的K线图窗口")

    def copy_stock_code(self):
        if self.selected_stock["code"]:
            self.master.clipboard_clear()
            self.master.clipboard_append(self.selected_stock["code"])
            self.status_label.config(text=f"已复制股票代码: {self.selected_stock['code']}")

    def copy_stock_name(self):
        if self.selected_stock["name"]:
            self.master.clipboard_clear()
            self.master.clipboard_append(self.selected_stock["name"])
            self.status_label.config(text=f"已复制股票名称: {self.selected_stock['name']}")

    def load_data(self):
        try:
            min_amount = int(self.amount_var.get())
        except ValueError:
            min_amount = 2000
            self.amount_var.set("2000")
        try:
            min_market_cap = int(self.market_cap_var.get())
        except ValueError:
            min_market_cap = 10
            self.market_cap_var.set("10")
        sort_by = self.sort_var.get()
        current_date = datetime.now().strftime('%Y%m%d')
        conn = sqlite3.connect('stock_data.db')
        query = f"""
        SELECT 
            a.代码,
            a.名称,
            b.交易所,
            b.行业,
            b.总市值,
            b.市场板块,
            b.今开,
            b.最新,
            b.涨幅,
            b.最低,
            b.最高,
            b.涨停,
            COUNT(1) AS 总成笔数,
            CAST(SUM(a.成交金额) / 10000 AS INTEGER) AS 总成交金额,
            GROUP_CONCAT(CAST(a.成交金额 / 10000 AS INTEGER) || '万(' || a.时间 || ')', '|') AS 时间金额明细
        FROM 
            stock_changes_{current_date} a,
            stock_real_data_{current_date} b
        WHERE 
            a.代码 = b.代码
            AND b.总市值 >= {min_market_cap}
        GROUP BY 
            a.代码,
            a.名称
        HAVING 
            总成交金额 > {min_amount}
        ORDER BY 
            {sort_by} DESC
        """
        full_df = pd.read_sql_query(query, conn)
        conn.close()
        save_to_excel(full_df)
        available_columns = [col for col in self.display_columns if col in full_df.columns]
        self.df = full_df[available_columns]
        self.update_table()

    def update_table(self):
        # 显示loading动画
        self.show_loading()

        # 使用after方法在下一个事件循环中执行表格更新，确保loading动画能显示
        self.master.after(10, self._update_table_content)

    def _update_table_content(self):
        """实际的表格更新内容"""
        try:
            # 清空现有表格内容
            for i in self.tree.get_children():
                self.tree.delete(i)

            # 更新表格列
            columns = list(self.df.columns)
            self.tree["columns"] = columns

            # 设置列宽
            col_widths = {
                "代码": 120, "名称": 120, "交易所": 60, "市场板块": 80, "总市值": 80,
                "今开": 70, "涨幅": 70, "最低": 70, "最高": 70, "涨停": 70,
                "总成笔数": 80, "总成交金额": 100, "时间金额明细": 200
            }

            for col in columns:
                self.tree.heading(col, text=col)
                self.tree.column(col, width=col_widths.get(col, 100), anchor="center")

            # 分批插入数据，让用户能看到加载过程
            self._insert_data_batch(0, columns)

        except Exception as e:
            logging.error(f"更新表格内容失败: {e}")
            self.hide_loading()

    def _insert_data_batch(self, start_index, columns, batch_size=50):
        """分批插入数据，每批插入batch_size行"""
        try:
            end_index = min(start_index + batch_size, len(self.df))

            # 插入当前批次的数据
            if "涨幅" in columns:
                change_idx = columns.index("涨幅")
                for i in range(start_index, end_index):
                    row = self.df.iloc[i]
                    item = self.tree.insert("", "end", values=list(row))
                    try:
                        change = float(row["涨幅"])
                        if change > 0:
                            self.tree.tag_configure(f"up_{item}", foreground='red', font=self.bold_font)
                            self.tree.item(item, tags=(f"up_{item}",))
                        elif change < 0:
                            self.tree.tag_configure(f"down_{item}", foreground='green', font=self.bold_font)
                            self.tree.item(item, tags=(f"down_{item}",))
                        else:
                            self.tree.tag_configure(f"zero_{item}", foreground='gray', font=self.normal_font)
                            self.tree.item(item, tags=(f"zero_{item}",))
                    except ValueError:
                        pass
            else:
                for i in range(start_index, end_index):
                    row = self.df.iloc[i]
                    self.tree.insert("", "end", values=list(row))

            # 更新界面显示
            self.tree.update_idletasks()

            # 如果还有更多数据，继续下一批
            if end_index < len(self.df):
                # 使用较短的延迟继续下一批，让用户能看到加载过程
                self.master.after(20, lambda: self._insert_data_batch(end_index, columns, batch_size))
            else:
                # 所有数据加载完成，隐藏loading动画
                self._finish_table_update()

        except Exception as e:
            logging.error(f"批量插入数据失败: {e}")
            self.hide_loading()

    def _finish_table_update(self):
        """完成表格更新的最后步骤"""
        try:
            self.tree.update_idletasks()
            self.vsb.lift()
            self.hsb.lift()
        finally:
            # 隐藏loading动画
            self.hide_loading()

    def show_detail(self, event):
        item = self.tree.selection()[0]
        values = self.tree.item(item, "values")
        columns = self.tree["columns"]
        detail_window = tk.Toplevel(self.master)
        detail_window.title(f"{values[columns.index('名称')]} ({values[columns.index('代码')]}) 详细信息")
        self.center_window(detail_window, 600, 400)
        text = tk.Text(detail_window, wrap=tk.WORD)
        text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        info_lines = [f"{col}: {value}" for col, value in zip(columns, values)]
        info = "\n".join(info_lines)
        text.insert(tk.END, info)
        if "涨幅" in columns:
            try:
                change_idx = columns.index("涨幅")
                change = float(values[change_idx])
                color = 'red' if change > 0 else 'green' if change < 0 else 'gray'
                font = ('Microsoft YaHei', 10, 'bold') if change != 0 else ('Microsoft YaHei', 10, 'normal')
                for i, line in enumerate(info_lines, 1):
                    if line.startswith("涨幅:"):
                        text.tag_add("change", f"{i}.0", f"{i}.0 lineend")
                        text.tag_config("change", foreground=color, font=font)
                        break
            except (ValueError, IndexError):
                pass
        text.config(state=tk.DISABLED)

    def __del__(self):
        """清理资源"""
        if hasattr(self, 'kline_executor'):
            self.kline_executor.shutdown(wait=False)


if __name__ == "__main__":
    root = tk.Tk()
    try:
        root.iconbitmap(default="logo.ico")
    except:
        pass  # 如果图标文件不存在，忽略错误
    app = StockVisualizationApp(root)


    # 定期清理已关闭的K线图窗口
    def periodic_cleanup():
        app.cleanup_closed_windows()
        root.after(30000, periodic_cleanup)  # 每30秒清理一次


    root.after(30000, periodic_cleanup)
    root.mainloop()