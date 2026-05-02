import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import sqlite3
from datetime import date, datetime, timedelta
import os
import bcrypt
import time
import shutil
import json
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm

# =====================================================
#   إعداد قاعدة البيانات
# =====================================================

DB_FOLDER = os.path.join(os.path.expanduser('~'), 'KingMattress_Data')
DB_NAME = os.path.join(DB_FOLDER, "kingmattres.db")
BACKUP_FOLDER = os.path.join(DB_FOLDER, 'backups')

if not os.path.exists(DB_FOLDER):
    os.makedirs(DB_FOLDER)
if not os.path.exists(BACKUP_FOLDER):
    os.makedirs(BACKUP_FOLDER)

CURRENCY_SYP = "ل.س"
CURRENCY_USD = "$"

# اتصال singleton - يمنع database is locked نهائياً
_db_conn = None

def get_conn():
    """إرجاع الاتصال المشترك الوحيد بقاعدة البيانات"""
    global _db_conn
    if _db_conn is None:
        _db_conn = sqlite3.connect(DB_NAME, timeout=30, check_same_thread=False)
        _db_conn.execute("PRAGMA journal_mode=WAL")
        _db_conn.execute("PRAGMA synchronous=NORMAL")
        _db_conn.execute("PRAGMA foreign_keys=ON")
    return _db_conn

def close_conn():
    """إغلاق الاتصال عند إنهاء التطبيق"""
    global _db_conn
    if _db_conn:
        try:
            _db_conn.commit()
            _db_conn.close()
        except Exception:
            pass
        _db_conn = None

def hash_password(password):
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode(), salt).decode()

def verify_password(password, hashed):
    return bcrypt.checkpw(password.encode(), hashed.encode())

def log_audit(user, action, table_name, record_id, old_data=None, new_data=None, conn=None):
    """تسجيل العمليات في سجل التدقيق"""
    c = conn if conn else get_conn()
    c.execute("""
        INSERT INTO audit_log (user, action, table_name, record_id, old_data, new_data, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user, action, table_name, record_id,
          json.dumps(old_data, ensure_ascii=False) if old_data else None,
          json.dumps(new_data, ensure_ascii=False) if new_data else None,
          datetime.now().isoformat()))

def backup_database():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_FOLDER, f"backup_{timestamp}.db")
    shutil.copy2(DB_NAME, backup_path)
    backups = sorted([f for f in os.listdir(BACKUP_FOLDER) if f.startswith("backup_")])
    while len(backups) > 30:
        os.remove(os.path.join(BACKUP_FOLDER, backups.pop(0)))
    return backup_path

def get_customer_balance(customer_id):
    conn = get_conn()
    total_invoices = conn.execute("""
        SELECT COALESCE(SUM(total_syp - paid_syp), 0)
        FROM invoices
        WHERE customer_id = ? AND status != 'مدفوعة'
    """, (customer_id,)).fetchone()[0]
    total_receipts = conn.execute("""
        SELECT COALESCE(SUM(amount_syp), 0)
        FROM receipt_vouchers
        WHERE customer_id = ?
    """, (customer_id,)).fetchone()[0]
    return total_receipts - total_invoices

def get_supplier_balance(supplier_id):
    conn = get_conn()
    total_purchases = conn.execute("""
        SELECT COALESCE(SUM(total_syp - paid_amount_syp), 0)
        FROM purchases
        WHERE supplier_id = ?
    """, (supplier_id,)).fetchone()[0]
    total_payments = conn.execute("""
        SELECT COALESCE(SUM(amount_syp), 0)
        FROM payment_vouchers
        WHERE supplier_id = ?
    """, (supplier_id,)).fetchone()[0]
    return total_purchases - total_payments

def generate_invoice_pdf(invoice_id):
    conn = get_conn()
    invoice = conn.execute("""
        SELECT i.invoice_number, i.invoice_date, i.due_date, i.payment_type,
               i.discount_percent, i.tax_percent, i.total_syp, i.notes, i.status,
               c.name, c.phone, c.address
        FROM invoices i
        LEFT JOIN customers c ON i.customer_id = c.id
        WHERE i.id = ?
    """, (invoice_id,)).fetchone()
    if not invoice:
        return None
    details = conn.execute("""
        SELECT p.name, id.quantity, id.price_syp, 
               (id.quantity * id.price_syp) as subtotal
        FROM invoice_details id
        JOIN products p ON id.product_id = p.id
        WHERE id.invoice_id = ?
    """, (invoice_id,)).fetchall()
    pass  # singleton - لا يُغلق الاتصال
    filename = os.path.join(DB_FOLDER, f"invoice_{invoice[0]}.pdf")
    doc = SimpleDocTemplate(filename, pagesize=A4, rightMargin=2*cm, leftMargin=2*cm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('TitleStyle', parent=styles['Title'], fontSize=16, alignment=1)
    story = []
    story.append(Paragraph("كينج ماتريس - فاتورة بيع", title_style))
    story.append(Spacer(1, 0.5*cm))
    info_data = [
        ['رقم الفاتورة:', invoice[0]],
        ['التاريخ:', invoice[1]],
        ['تاريخ الاستحقاق:', invoice[2] or '-'],
        ['العميل:', invoice[9]],
        ['الهاتف:', invoice[10] or '-'],
        ['العنوان:', invoice[11] or '-'],
        ['نوع الدفع:', invoice[3]],
        ['الحالة:', invoice[8]],
    ]
    info_table = Table(info_data, colWidths=[4*cm, 8*cm])
    info_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 0.5*cm))
    table_data = [['المنتج', 'الكمية', 'السعر', 'الإجمالي']]
    total = invoice[6]
    for d in details:
        row = [d[0], str(d[1]), f"{d[2]:,.0f}", f"{d[3]:,.0f}"]
        table_data.append(row)
    if invoice[4] > 0:
        discount_amount = total * invoice[4] / 100
        total -= discount_amount
        table_data.append(['', '', 'خصم', f"{discount_amount:,.0f}"])
    if invoice[5] > 0:
        tax_amount = total * invoice[5] / 100
        total += tax_amount
        table_data.append(['', '', 'ضريبة', f"{tax_amount:,.0f}"])
    table_data.append(['', '', 'الإجمالي', f"{total:,.0f}"])
    products_table = Table(table_data, colWidths=[6*cm, 2*cm, 3*cm, 3*cm])
    products_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
    ]))
    story.append(products_table)
    story.append(Spacer(1, 0.5*cm))
    if invoice[7]:
        story.append(Paragraph(f"ملاحظات: {invoice[7]}", styles['Normal']))
    doc.build(story)
    return filename

def init_db():
    conn = get_conn()
    c = conn.cursor()
    
    # جدول المستخدمين
    c.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        full_name TEXT,
        is_active INTEGER DEFAULT 1,
        last_login TEXT,
        created_at TEXT
    )''')
    
    # جدول الصلاحيات
    c.execute('''
    CREATE TABLE IF NOT EXISTS permissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        permission TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')
    
    # جدول العملاء
    c.execute('''
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT,
        email TEXT,
        address TEXT,
        reg_date TEXT,
        customer_type TEXT DEFAULT 'عادي',
        credit_limit REAL DEFAULT 0,
        notes TEXT
    )''')
    
    # جدول الموردين
    c.execute('''
    CREATE TABLE IF NOT EXISTS suppliers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT,
        email TEXT,
        address TEXT,
        reg_date TEXT,
        notes TEXT
    )''')
    
    # جدول المنتجات
    c.execute('''
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        type TEXT,
        description TEXT,
        price_syp REAL NOT NULL,
        price_usd REAL NOT NULL,
        cost_syp REAL,
        cost_usd REAL,
        unit TEXT DEFAULT 'قطعة',
        active INTEGER DEFAULT 1,
        location TEXT,
        barcode TEXT
    )''')
    
    # جدول الفواتير
    c.execute('''
    CREATE TABLE IF NOT EXISTS invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_number TEXT UNIQUE,
        customer_id INTEGER,
        invoice_date TEXT NOT NULL,
        due_date TEXT,
        payment_type TEXT DEFAULT 'نقدي',
        discount_percent REAL DEFAULT 0,
        tax_percent REAL DEFAULT 0,
        total_syp REAL DEFAULT 0,
        paid_syp REAL DEFAULT 0,
        notes TEXT,
        status TEXT DEFAULT 'مفتوحة',
        created_by TEXT,
        created_at TEXT,
        exchange_rate REAL,
        FOREIGN KEY (customer_id) REFERENCES customers(id)
    )''')
    
    # تفاصيل الفاتورة
    c.execute('''
    CREATE TABLE IF NOT EXISTS invoice_details (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        quantity REAL DEFAULT 1,
        price_syp REAL,
        price_usd REAL,
        discount_percent REAL DEFAULT 0,
        FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE,
        FOREIGN KEY (product_id) REFERENCES products(id)
    )''')
    
    # جدول سندات القبض
    c.execute('''
    CREATE TABLE IF NOT EXISTS receipt_vouchers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        voucher_number TEXT UNIQUE,
        voucher_date TEXT NOT NULL,
        customer_id INTEGER,
        supplier_id INTEGER,
        amount_syp REAL DEFAULT 0,
        amount_usd REAL DEFAULT 0,
        payment_method TEXT,
        reference_invoice_id INTEGER,
        notes TEXT,
        created_by TEXT,
        created_at TEXT,
        FOREIGN KEY (customer_id) REFERENCES customers(id),
        FOREIGN KEY (supplier_id) REFERENCES suppliers(id)
    )''')
    
    # جدول سندات الدفع
    c.execute('''
    CREATE TABLE IF NOT EXISTS payment_vouchers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        voucher_number TEXT UNIQUE,
        voucher_date TEXT NOT NULL,
        customer_id INTEGER,
        supplier_id INTEGER,
        amount_syp REAL DEFAULT 0,
        amount_usd REAL DEFAULT 0,
        payment_method TEXT,
        reference_invoice_id INTEGER,
        notes TEXT,
        created_by TEXT,
        created_at TEXT,
        FOREIGN KEY (customer_id) REFERENCES customers(id),
        FOREIGN KEY (supplier_id) REFERENCES suppliers(id)
    )''')
    
    # جدول الصناديق
    c.execute('''
    CREATE TABLE IF NOT EXISTS cash_boxes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        box_name TEXT NOT NULL,
        currency TEXT NOT NULL,
        balance REAL DEFAULT 0
    )''')
    
    # جدول حركة الصندوق
    c.execute('''
    CREATE TABLE IF NOT EXISTS cash_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        movement_date TEXT NOT NULL,
        box_id INTEGER NOT NULL,
        movement_type TEXT NOT NULL,
        amount REAL NOT NULL,
        reference_type TEXT,
        reference_id INTEGER,
        notes TEXT,
        created_by TEXT,
        FOREIGN KEY (box_id) REFERENCES cash_boxes(id)
    )''')
    
    # جدول المستودع
    c.execute('''
    CREATE TABLE IF NOT EXISTS warehouse (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        type TEXT,
        unit TEXT,
        quantity REAL DEFAULT 0,
        min_limit REAL DEFAULT 0,
        unit_price_syp REAL,
        unit_price_usd REAL,
        supplier_id INTEGER,
        location TEXT,
        notes TEXT,
        FOREIGN KEY (supplier_id) REFERENCES suppliers(id)
    )''')
    
    # جدول صرف المواد
    c.execute('''
    CREATE TABLE IF NOT EXISTS material_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        material_id INTEGER NOT NULL,
        quantity REAL,
        usage_date TEXT,
        reference TEXT,
        notes TEXT,
        FOREIGN KEY (material_id) REFERENCES warehouse(id)
    )''')
    
    # جدول المصاريف
    c.execute('''
    CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        expense_type TEXT,
        category TEXT,
        amount_syp REAL DEFAULT 0,
        amount_usd REAL DEFAULT 0,
        expense_date TEXT NOT NULL,
        notes TEXT,
        created_by TEXT
    )''')
    
    # جدول المشتريات
    c.execute('''
    CREATE TABLE IF NOT EXISTS purchases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        purchase_number TEXT UNIQUE,
        supplier_id INTEGER,
        purchase_date TEXT NOT NULL,
        item_name TEXT,
        quantity REAL,
        unit_price_syp REAL,
        unit_price_usd REAL,
        total_syp REAL,
        total_usd REAL,
        payment_type TEXT DEFAULT 'نقدي',
        paid_amount_syp REAL DEFAULT 0,
        paid_amount_usd REAL DEFAULT 0,
        notes TEXT,
        created_by TEXT,
        FOREIGN KEY (supplier_id) REFERENCES suppliers(id)
    )''')
    
    # جدول الموظفين
    c.execute('''
    CREATE TABLE IF NOT EXISTS employees (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        job TEXT,
        phone TEXT,
        salary_syp REAL,
        salary_usd REAL,
        hire_date TEXT,
        status TEXT DEFAULT 'نشط',
        notes TEXT
    )''')
    
    # جدول سجل التدقيق
    c.execute('''
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user TEXT NOT NULL,
        action TEXT NOT NULL,
        table_name TEXT,
        record_id INTEGER,
        old_data TEXT,
        new_data TEXT,
        timestamp TEXT NOT NULL
    )''')
    
    # بيانات افتراضية
    admin_exists = c.execute("SELECT COUNT(*) FROM users WHERE username='admin'").fetchone()[0]
    if admin_exists == 0:
        c.execute("INSERT INTO users (username, password, full_name, created_at) VALUES (?, ?, ?, ?)",
                  ('admin', hash_password('admin123'), 'مدير النظام', datetime.now().isoformat()))
        admin_id = c.lastrowid
        permissions = [
            'view_customers', 'edit_customers', 'delete_customers',
            'view_invoices', 'edit_invoices', 'delete_invoices',
            'view_products', 'edit_products', 'delete_products',
            'view_warehouse', 'edit_warehouse', 'delete_warehouse',
            'view_receipts', 'edit_receipts', 'delete_receipts',
            'view_payments', 'edit_payments', 'delete_payments',
            'view_reports', 'export_reports',
            'view_users', 'edit_users', 'delete_users',
            'backup_restore'
        ]
        for perm in permissions:
            c.execute("INSERT INTO permissions (user_id, permission) VALUES (?, ?)", (admin_id, perm))
    
    boxes_count = c.execute("SELECT COUNT(*) FROM cash_boxes").fetchone()[0]
    if boxes_count == 0:
        c.execute("INSERT INTO cash_boxes (box_name, currency, balance) VALUES (?, ?, ?)",
                  ('صندوق الليرة', 'SYP', 0))
        c.execute("INSERT INTO cash_boxes (box_name, currency, balance) VALUES (?, ?, ?)",
                  ('صندوق الدولار', 'USD', 0))
    
    customers_count = c.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    if customers_count == 0:
        c.execute("INSERT INTO customers (name, phone, address, reg_date, customer_type) VALUES (?, ?, ?, ?, ?)",
                  ('أحمد محمد', '0501234567', 'الرياض - حي النزهة', date.today().isoformat(), 'جملة'))
        c.execute("INSERT INTO customers (name, phone, address, reg_date, customer_type) VALUES (?, ?, ?, ?, ?)",
                  ('فاطمة السالم', '0559876543', 'جدة - حي الروضة', date.today().isoformat(), 'تجزئة'))
    
    suppliers_count = c.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0]
    if suppliers_count == 0:
        c.execute("INSERT INTO suppliers (name, phone, address, notes) VALUES (?, ?, ?, ?)",
                  ('شركة الخشب الذهبي', '0112345678', 'الرياض - الصناعية', 'مورد خشب'))
        c.execute("INSERT INTO suppliers (name, phone, address, notes) VALUES (?, ?, ?, ?)",
                  ('مصنع النسيج المتحد', '0118765432', 'جدة - المنطقة الصناعية', 'مورد قماش'))
    
    products_count = c.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    if products_count == 0:
        c.execute("INSERT INTO products (name, type, price_syp, price_usd, cost_syp, cost_usd, location) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  ('كنبة 3 مقاعد', 'كنبة', 5000000, 2500, 2500000, 1200, 'الرف A1'))
        c.execute("INSERT INTO products (name, type, price_syp, price_usd, cost_syp, cost_usd, location) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  ('سرير 160×200', 'سرير', 3600000, 1800, 1800000, 900, 'الرف B2'))
        c.execute("INSERT INTO products (name, type, price_syp, price_usd, cost_syp, cost_usd, location) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  ('كرسي مكتب', 'كرسي', 900000, 450, 400000, 200, 'الرف C3'))
    
    warehouse_count = c.execute("SELECT COUNT(*) FROM warehouse").fetchone()[0]
    if warehouse_count == 0:
        c.execute("INSERT INTO warehouse (name, type, unit, quantity, min_limit, unit_price_syp, unit_price_usd, location) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  ('خشب بلوط', 'خشب', 'متر مربع', 100, 20, 120000, 60, 'المستودع 1'))
        c.execute("INSERT INTO warehouse (name, type, unit, quantity, min_limit, unit_price_syp, unit_price_usd, location) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  ('قماش مخمل', 'قماش', 'متر', 200, 30, 45000, 22.5, 'المستودع 2'))
        c.execute("INSERT INTO warehouse (name, type, unit, quantity, min_limit, unit_price_syp, unit_price_usd, location) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  ('إسفنج 10 سم', 'إسفنج', 'متر مربع', 80, 15, 35000, 17.5, 'المستودع 1'))
    
    conn.commit()

# =====================================================
#   ألوان وخطوط التطبيق
# =====================================================
BG = "#1A1A2E"
CARD = "#16213E"
ACCENT = "#E94560"
ACCENT2 = "#0F3460"
TEXT = "#EAEAEA"
MUTED = "#8892A4"
SUCCESS = "#2ECC71"
WARNING = "#F39C12"
DANGER = "#E74C3C"

FONT_AR = ("Arial", 11)
FONT_BIG = ("Arial", 14, "bold")
FONT_SM = ("Arial", 9)
FONT_TITLE = ("Arial", 24, "bold")
# =====================================================
#   نافذة تسجيل الدخول
# =====================================================
class LoginWindow:
    def __init__(self, root):
        self.root = root
        self.root.title("تسجيل الدخول - كينج ماتريس")
        self.root.geometry("420x550")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        
        init_db()
        self.fade_in()
        
        self.login_frame = tk.Frame(self.root, bg=BG)
        self.login_frame.pack(fill="both", expand=True)
        
        tk.Label(self.login_frame, text="🛏️", font=("Arial", 60),
                 bg=BG, fg=ACCENT).pack(pady=(40, 10))
        tk.Label(self.login_frame, text="كينج ماتريس", font=FONT_TITLE,
                 bg=BG, fg=TEXT).pack()
        tk.Label(self.login_frame, text="نظام إدارة معمل المفروشات", 
                 font=FONT_AR, bg=BG, fg=MUTED).pack(pady=(5, 30))
        
        tk.Label(self.login_frame, text="اسم المستخدم", font=FONT_AR,
                 bg=BG, fg=TEXT).pack(anchor="w", padx=50, pady=(10, 5))
        self.username_entry = tk.Entry(self.login_frame, font=FONT_AR, 
                                        bg=ACCENT2, fg=TEXT, insertbackground=TEXT,
                                        relief="flat", width=30)
        self.username_entry.pack(padx=50, ipady=8)
        self.username_entry.bind("<Return>", lambda e: self.login())
        
        tk.Label(self.login_frame, text="كلمة المرور", font=FONT_AR,
                 bg=BG, fg=TEXT).pack(anchor="w", padx=50, pady=(10, 5))
        self.password_entry = tk.Entry(self.login_frame, font=FONT_AR,
                                        bg=ACCENT2, fg=TEXT, insertbackground=TEXT,
                                        relief="flat", width=30, show="•")
        self.password_entry.pack(padx=50, ipady=8)
        self.password_entry.bind("<Return>", lambda e: self.login())
        
        self.login_btn = tk.Button(self.login_frame, text="🔓 دخول", command=self.login,
                                    font=FONT_BIG, bg=ACCENT, fg="white",
                                    relief="flat", padx=40, pady=10, cursor="hand2")
        self.login_btn.pack(pady=30)
        
        def on_enter(e):
            self.login_btn.config(bg=SUCCESS, font=("Arial", 15, "bold"))
        def on_leave(e):
            self.login_btn.config(bg=ACCENT, font=FONT_BIG)
        
        self.login_btn.bind("<Enter>", on_enter)
        self.login_btn.bind("<Leave>", on_leave)
        
        tk.Label(self.login_frame, text="البيانات الافتراضية: admin / admin123", 
                 font=FONT_SM, bg=BG, fg=MUTED).pack(pady=10)
        self.show_overdue_alerts()
    
    def show_overdue_alerts(self):
        conn = get_conn()
        today = date.today().isoformat()
        try:
            overdue_invoices = conn.execute("""
                SELECT i.id, c.name, i.due_date, (i.total_syp - i.paid_syp) as remaining
                FROM invoices i
                JOIN customers c ON i.customer_id = c.id
                WHERE i.due_date < ? AND (i.total_syp - i.paid_syp) > 0 AND i.status != 'مدفوعة'
                ORDER BY i.due_date
            """, (today,)).fetchall()
        finally:
            pass  # singleton - لا يُغلق الاتصال
        if overdue_invoices:
            msg = "⚠️ تنبيه: هناك فواتير متأخرة:\n\n"
            for inv in overdue_invoices[:5]:
                msg += f"• {inv[1]} - فاتورة رقم {inv[0]} - تاريخ الاستحقاق: {inv[2]}\n"
            if len(overdue_invoices) > 5:
                msg += f"\nو {len(overdue_invoices) - 5} فواتير أخرى..."
            messagebox.showwarning("ديون متأخرة", msg)
    
    def fade_in(self):
        alpha = 0
        self.root.attributes('-alpha', alpha)
        def increase():
            nonlocal alpha
            alpha += 0.05
            if alpha <= 1:
                self.root.attributes('-alpha', alpha)
                self.root.after(30, increase)
        increase()
    
    def login(self):
        username = self.username_entry.get().strip()
        password = self.password_entry.get().strip()
        if not username or not password:
            messagebox.showerror("خطأ", "الرجاء إدخال اسم المستخدم وكلمة المرور")
            return
        conn = get_conn()
        user = conn.execute("SELECT id, username, password, full_name FROM users WHERE username=? AND is_active=1", 
                           (username,)).fetchone()
        if user and verify_password(password, user[2]):
            conn.execute("UPDATE users SET last_login=? WHERE id=?", (datetime.now().isoformat(), user[0]))
            permissions = [p[0] for p in conn.execute("SELECT permission FROM permissions WHERE user_id=?", (user[0],)).fetchall()]
            conn.commit()
            pass  # singleton - لا يُغلق الاتصال
            self.root.destroy()
            root = tk.Tk()
            app = MafroshatApp(root, user[1], user[3], permissions)
            root.mainloop()
        else:
            messagebox.showerror("خطأ", "اسم المستخدم أو كلمة المرور غير صحيحة")
            self.password_entry.delete(0, tk.END)
            pass  # singleton - لا يُغلق الاتصال

# =====================================================
#   التطبيق الرئيسي
# =====================================================
class MafroshatApp:
    def __init__(self, root, username, full_name, permissions):
        self.root = root
        self.username = username
        self.full_name = full_name
        self.permissions = permissions
        self.root.title(f"كينج ماتريس - مرحباً {full_name}")
        self.root.geometry("1400x800")
        self.root.configure(bg=BG)
        self.root.resizable(True, True)
        self._style()
        self._top_bar()
        self._sidebar()
        self._content_area()
        self.show_welcome()
        self.show_page("home")
    
    def has_permission(self, permission):
        return 'admin' in self.permissions or permission in self.permissions
    
    def show_welcome(self):
        welcome = tk.Toplevel(self.root)
        welcome.title("مرحباً")
        welcome.geometry("500x450")
        welcome.configure(bg=BG)
        welcome.transient(self.root)
        welcome.grab_set()
        welcome.attributes('-alpha', 0)
        alpha = 0
        def fade_in():
            nonlocal alpha
            alpha += 0.05
            if alpha <= 1:
                welcome.attributes('-alpha', alpha)
                welcome.after(30, fade_in)
        fade_in()
        tk.Label(welcome, text="🛏️", font=("Arial", 50), bg=BG, fg=ACCENT).pack(pady=(30, 10))
        tk.Label(welcome, text=f"مرحباً بك, {self.full_name}!", font=FONT_TITLE, bg=BG, fg=TEXT).pack()
        now = datetime.now()
        days_ar = {"Monday": "الإثنين", "Tuesday": "الثلاثاء", "Wednesday": "الأربعاء",
                   "Thursday": "الخميس", "Friday": "الجمعة", "Saturday": "السبت", "Sunday": "الأحد"}
        months_ar = {"January": "يناير", "February": "فبراير", "March": "مارس", "April": "أبريل",
                     "May": "مايو", "June": "يونيو", "July": "يوليو", "August": "أغسطس",
                     "September": "سبتمبر", "October": "أكتوبر", "November": "نوفمبر", "December": "ديسمبر"}
        day_ar = days_ar.get(now.strftime("%A"), now.strftime("%A"))
        month_ar = months_ar.get(now.strftime("%B"), now.strftime("%B"))
        tk.Label(welcome, text=f"{day_ar}، {now.strftime('%d')} {month_ar} {now.strftime('%Y')}", 
                 font=FONT_AR, bg=BG, fg=MUTED).pack(pady=5)
        tk.Label(welcome, text=now.strftime("%I:%M:%S %p"), font=("Arial", 18), bg=BG, fg=SUCCESS).pack()
        tk.Frame(welcome, bg=ACCENT, height=2).pack(fill="x", padx=40, pady=20)
        conn = get_conn()
        today = date.today().isoformat()
        try:
            today_invoices = conn.execute("SELECT COUNT(*) FROM invoices WHERE invoice_date=?", (today,)).fetchone()[0]
            total_customers = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
            total_suppliers = conn.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0]
            overdue = conn.execute("SELECT COUNT(*) FROM invoices WHERE due_date < ? AND (total_syp - paid_syp) > 0 AND status != 'مدفوعة'", (today,)).fetchone()[0]
            box_syp = conn.execute("SELECT balance FROM cash_boxes WHERE currency='SYP'").fetchone()
            box_usd = conn.execute("SELECT balance FROM cash_boxes WHERE currency='USD'").fetchone()
        finally:
            pass  # singleton - لا يُغلق الاتصال
        stats_frame = tk.Frame(welcome, bg=BG)
        stats_frame.pack(pady=10)
        tk.Label(stats_frame, text=f"📦 فواتير اليوم: {today_invoices}", font=FONT_AR, bg=BG, fg=SUCCESS).pack()
        tk.Label(stats_frame, text=f"👥 إجمالي العملاء: {total_customers}", font=FONT_AR, bg=BG, fg=WARNING).pack()
        tk.Label(stats_frame, text=f"🏭 إجمالي الموردين: {total_suppliers}", font=FONT_AR, bg=BG, fg=ACCENT).pack()
        tk.Label(stats_frame, text=f"⚠️ ديون متأخرة: {overdue}", font=FONT_AR, bg=BG, fg=DANGER if overdue > 0 else SUCCESS).pack()
        tk.Label(stats_frame, text=f"💰 رصيد صندوق الليرة: {box_syp[0]:,.0f} {CURRENCY_SYP}" if box_syp else "💰 صندوق الليرة: 0", font=FONT_AR, bg=BG, fg=SUCCESS).pack()
        tk.Label(stats_frame, text=f"💵 رصيد صندوق الدولار: {box_usd[0]:,.2f} {CURRENCY_USD}" if box_usd else "💵 صندوق الدولار: 0", font=FONT_AR, bg=BG, fg=SUCCESS).pack()
        def close():
            for i in range(10, 0, -1):
                welcome.attributes('-alpha', i/10)
                welcome.update()
                time.sleep(0.03)
            welcome.destroy()
        btn = tk.Button(welcome, text="ابدأ", command=close, font=FONT_BIG, bg=ACCENT, fg="white", relief="flat", padx=30, pady=8, cursor="hand2")
        btn.pack(pady=20)
        self.root.after(4000, close)
    
    def _style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background=CARD, foreground=TEXT, fieldbackground=CARD, rowheight=32, font=FONT_AR)
        style.configure("Treeview.Heading", background=ACCENT2, foreground=TEXT, font=("Arial", 10, "bold"))
        style.map("Treeview", background=[("selected", ACCENT)])
    
    def _top_bar(self):
        top = tk.Frame(self.root, bg=ACCENT, height=50)
        top.pack(side="top", fill="x")
        top.pack_propagate(False)
        tk.Label(top, text="🛏️ كينج ماتريس - نظام إدارة معمل المفروشات", font=("Arial", 14, "bold"), bg=ACCENT, fg="white").pack(side="left", padx=20)
        conn = get_conn()
        try:
            box_syp = conn.execute("SELECT balance FROM cash_boxes WHERE currency='SYP'").fetchone()
            box_usd = conn.execute("SELECT balance FROM cash_boxes WHERE currency='USD'").fetchone()
        finally:
            pass  # singleton - لا يُغلق الاتصال
        balance_frame = tk.Frame(top, bg=ACCENT)
        balance_frame.pack(side="right", padx=20)
        tk.Label(balance_frame, text=f"💰 {box_syp[0]:,.0f} {CURRENCY_SYP}" if box_syp else "💰 0", font=FONT_SM, bg=ACCENT, fg=SUCCESS).pack(side="left", padx=10)
        tk.Label(balance_frame, text=f"💵 {box_usd[0]:,.2f} {CURRENCY_USD}" if box_usd else "💵 0", font=FONT_SM, bg=ACCENT, fg=SUCCESS).pack(side="left", padx=10)
        user_frame = tk.Frame(top, bg=ACCENT)
        user_frame.pack(side="right", padx=20)
        tk.Label(user_frame, text=f"👤 {self.full_name}", font=FONT_AR, bg=ACCENT, fg="white").pack(side="left")
        if self.has_permission('backup_restore'):
            backup_btn = tk.Button(user_frame, text="💾 نسخ احتياطي", command=self.do_backup, font=FONT_SM, bg=SUCCESS, fg="white", relief="flat", padx=10, pady=3, cursor="hand2")
            backup_btn.pack(side="left", padx=10)
        logout_btn = tk.Button(user_frame, text="🚪 خروج", command=self.logout, font=FONT_SM, bg=DANGER, fg="white", relief="flat", padx=10, pady=3, cursor="hand2")
        logout_btn.pack(side="left", padx=10)
        self.time_label = tk.Label(top, font=FONT_AR, bg=ACCENT, fg="white")
        self.time_label.pack(side="right", padx=20)
        self.update_time()
    
    def do_backup(self):
        try:
            backup_path = backup_database()
            messagebox.showinfo("نجاح", f"تم إنشاء النسخة الاحتياطية بنجاح!\nالموقع: {backup_path}")
            log_audit(self.username, "BACKUP", "database", 0, None, backup_path)
        except Exception as e:
            messagebox.showerror("خطأ", f"فشل إنشاء النسخة الاحتياطية: {str(e)}")
    
    def logout(self):
        if messagebox.askyesno("تسجيل خروج", "هل أنت متأكد من تسجيل الخروج؟"):
            self.root.destroy()
            root = tk.Tk()
            LoginWindow(root)
            root.mainloop()
    
    def update_time(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.time_label.config(text=f"📅 {now}")
        self.root.after(1000, self.update_time)
        
    def _sidebar(self):
        self.sidebar = tk.Frame(self.root, bg=CARD, width=280)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        tk.Label(self.sidebar, text="🛏️", font=("Arial", 40), bg=CARD, fg=ACCENT).pack(pady=(20, 0))
        tk.Label(self.sidebar, text="كينج ماتريس", font=("Arial", 12, "bold"), bg=CARD, fg=TEXT).pack(pady=(5, 10))
        tk.Frame(self.sidebar, bg=ACCENT, height=2).pack(fill="x", padx=20, pady=5)
        
        all_pages = [
            ("🏠 الرئيسية", "home", True),
            ("👥 العملاء", "customers", self.has_permission('view_customers')),
            ("🏭 الموردين", "suppliers", self.has_permission('view_customers')),
            ("📄 الفواتير", "invoices", self.has_permission('view_invoices')),
            ("🛠️ المنتجات", "products", self.has_permission('view_products')),
            ("🏪 المستودع", "warehouse", self.has_permission('view_warehouse')),
            ("💰 سندات قبض", "receipts", self.has_permission('view_receipts')),
            ("💸 سندات دفع", "payments", self.has_permission('view_payments')),
            ("🏦 الصناديق", "cash_boxes", True),
            ("📊 التقارير", "reports", self.has_permission('view_reports')),
            ("👥 المستخدمين", "users", self.has_permission('view_users')),
            ("📋 سجل التدقيق", "audit_log", self.has_permission('view_users')),
        ]
        
        self.nav_buttons = {}
        for label, page, visible in all_pages:
            if visible:
                btn = tk.Button(self.sidebar, text=label, font=FONT_AR, bg=CARD, fg=TEXT, 
                               activebackground=ACCENT, activeforeground="white", relief="flat", 
                               anchor="w", padx=25, pady=8, cursor="hand2", 
                               command=lambda p=page: self.show_page(p))
                btn.pack(fill="x", pady=2)
                def on_enter(e, b=btn):
                    b.config(bg=ACCENT2, font=("Arial", 11, "bold"))
                def on_leave(e, b=btn):
                    b.config(bg=CARD, font=FONT_AR)
                btn.bind("<Enter>", on_enter)
                btn.bind("<Leave>", on_leave)
                self.nav_buttons[page] = btn

    def _content_area(self):
        self.content = tk.Frame(self.root, bg=BG)
        self.content.pack(side="left", fill="both", expand=True)

    def clear_content(self):
        for w in self.content.winfo_children():
            w.destroy()

    def show_page(self, page):
        for p, btn in self.nav_buttons.items():
            btn.configure(bg=ACCENT if p == page else CARD)
        self.clear_content()
        pages = {
            "home": self.page_home,
            "customers": self.page_customers,
            "suppliers": self.page_suppliers,
            "invoices": self.page_invoices,
            "products": self.page_products,
            "warehouse": self.page_warehouse,
            "receipts": self.page_receipts,
            "payments": self.page_payments,
            "cash_boxes": self.page_cash_boxes,
            "reports": self.page_reports,
            "users": self.page_users,
            "audit_log": self.page_audit_log,
        }
        pages.get(page, self.page_home)()

    # =====================================================
    #   صفحة الرئيسية
    # =====================================================
    def page_home(self):
        tk.Label(self.content, text="لوحة التحكم الرئيسية", font=("Arial", 20, "bold"), 
                 bg=BG, fg=ACCENT).pack(pady=(20, 5), anchor="center")
        cards_frame = tk.Frame(self.content, bg=BG)
        cards_frame.pack(fill="x", padx=30, pady=20)
        
        conn = get_conn()
        today = date.today().isoformat()
        try:
            today_invoices = conn.execute("SELECT COUNT(*) FROM invoices WHERE invoice_date=?", (today,)).fetchone()[0]
            today_sales = conn.execute("SELECT COALESCE(SUM(total_syp),0) FROM invoices WHERE invoice_date=?", (today,)).fetchone()[0]
            total_customers = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
            total_suppliers = conn.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0]
            total_products = conn.execute("SELECT COUNT(*) FROM products WHERE active=1").fetchone()[0]
            overdue_debt = conn.execute("SELECT COALESCE(SUM(total_syp - paid_syp),0) FROM invoices WHERE due_date < ? AND (total_syp - paid_syp) > 0 AND status != 'مدفوعة'", (today,)).fetchone()[0]
            low_stock = conn.execute("SELECT COUNT(*) FROM warehouse WHERE quantity <= min_limit").fetchone()[0]
            box_syp = conn.execute("SELECT balance FROM cash_boxes WHERE currency='SYP'").fetchone()
            box_usd = conn.execute("SELECT balance FROM cash_boxes WHERE currency='USD'").fetchone()
        finally:
            pass  # singleton - لا يُغلق الاتصال
        
        stats = [
            ("📄 فواتير اليوم", today_invoices, ACCENT),
            ("💰 مبيعات اليوم", f"{today_sales:,.0f} {CURRENCY_SYP}", SUCCESS),
            ("👥 العملاء", total_customers, WARNING),
            ("🏭 الموردين", total_suppliers, ACCENT),
            ("🛠️ المنتجات", total_products, "#9B59B6"),
            ("⚠️ ديون متأخرة", f"{overdue_debt:,.0f} {CURRENCY_SYP}", DANGER if overdue_debt > 0 else SUCCESS),
            ("🏪 مواد منخفضة", low_stock, DANGER if low_stock > 0 else SUCCESS),
            ("🏦 صندوق الليرة", f"{box_syp[0]:,.0f} {CURRENCY_SYP}" if box_syp else "0", SUCCESS),
            ("💵 صندوق الدولار", f"{box_usd[0]:,.2f} {CURRENCY_USD}" if box_usd else "0", SUCCESS),
        ]
        
        for i, (title, val, color) in enumerate(stats):
            card = tk.Frame(cards_frame, bg=CARD, relief="raised", bd=1)
            row = i // 3
            col = i % 3
            card.grid(row=row, column=col, padx=10, pady=10, sticky="nsew")
            cards_frame.columnconfigure(col, weight=1)
            cards_frame.rowconfigure(row, weight=1)
            tk.Label(card, text=str(val), font=("Arial", 14, "bold"), bg=CARD, fg=color).pack(pady=(15, 5))
            tk.Label(card, text=title, font=FONT_SM, bg=CARD, fg=TEXT).pack(pady=(0, 15))
        
        tk.Label(self.content, text="📋 آخر الفواتير", font=FONT_BIG, bg=BG, fg=TEXT).pack(anchor="w", padx=30, pady=(20, 10))
        frame = tk.Frame(self.content, bg=BG)
        frame.pack(fill="both", expand=True, padx=30)
        cols = ("الرقم", "رقم الفاتورة", "العميل", "التاريخ", "نوع الدفع", "الإجمالي", "الحالة")
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=6)
        for col in cols:
            tree.heading(col, text=col)
            tree.column(col, width=130, anchor="center")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        
        conn = get_conn()
        try:
            rows = conn.execute("""
                SELECT i.id, i.invoice_number, c.name, i.invoice_date, i.payment_type, i.total_syp, i.status
                FROM invoices i
                LEFT JOIN customers c ON i.customer_id = c.id
                ORDER BY i.id DESC LIMIT 10
            """).fetchall()
        finally:
            pass  # singleton - لا يُغلق الاتصال
        
        for row in rows:
            values = list(row)
            values[5] = f"{values[5]:,.0f} {CURRENCY_SYP}"
            tree.insert("", "end", values=values)

    # =====================================================
    #   صفحة العملاء
    # =====================================================
    def page_customers(self):
        self._page_header("👥 إدارة العملاء", self._add_customer_dialog if self.has_permission('edit_customers') else None)
        
        search_frame = tk.Frame(self.content, bg=BG)
        search_frame.pack(fill="x", padx=20, pady=5)
        tk.Label(search_frame, text="بحث:", bg=BG, fg=TEXT).pack(side="left", padx=5)
        self.search_entry = tk.Entry(search_frame, font=FONT_AR, bg=ACCENT2, fg=TEXT, width=20)
        self.search_entry.pack(side="left", padx=5)
        self.search_entry.bind("<KeyRelease>", lambda e: self._load_customers())
        self._btn(search_frame, "بحث", self._load_customers, ACCENT).pack(side="left", padx=5)
        
        frame = tk.Frame(self.content, bg=BG)
        frame.pack(fill="both", expand=True, padx=20, pady=5)
        cols = ("الرقم", "الاسم", "الهاتف", "العنوان", "النوع", "الرصيد")
        self.cust_tree = ttk.Treeview(frame, columns=cols, show="headings")
        for col in cols:
            self.cust_tree.heading(col, text=col)
            self.cust_tree.column(col, width=140, anchor="center")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.cust_tree.yview)
        self.cust_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.cust_tree.pack(fill="both", expand=True)
        
        btn_frame = tk.Frame(self.content, bg=BG)
        btn_frame.pack(pady=8)
        if self.has_permission('edit_customers'):
            self._btn(btn_frame, "✏️ تعديل", self._edit_customer, WARNING).pack(side="right", padx=5)
        if self.has_permission('delete_customers'):
            self._btn(btn_frame, "🗑️ حذف", self._delete_customer, DANGER).pack(side="right", padx=5)
        self._btn(btn_frame, "📊 حساب العميل", self._show_customer_account, ACCENT).pack(side="right", padx=5)
        self._load_customers()

    def _load_customers(self):
        self.cust_tree.delete(*self.cust_tree.get_children())
        keyword = self.search_entry.get().strip()
        conn = get_conn()
        try:
            if keyword:
                rows = conn.execute("SELECT id, name, phone, address, customer_type FROM customers WHERE name LIKE ? OR phone LIKE ?", 
                                   (f'%{keyword}%', f'%{keyword}%')).fetchall()
            else:
                rows = conn.execute("SELECT id, name, phone, address, customer_type FROM customers").fetchall()
        finally:
            pass  # singleton - لا يُغلق الاتصال
        
        for row in rows:
            balance = get_customer_balance(row[0])
            if balance < 0:
                balance_text = f"{abs(balance):,.0f} {CURRENCY_SYP} (مدين)"
                color = DANGER
            else:
                balance_text = f"{balance:,.0f} {CURRENCY_SYP} (دائن)"
                color = SUCCESS
            values = (row[0], row[1], row[2], row[3], row[4], balance_text)
            self.cust_tree.insert("", "end", values=values, tags=(color,))
        self.cust_tree.tag_configure(DANGER, foreground=DANGER)
        self.cust_tree.tag_configure(SUCCESS, foreground=SUCCESS)

    def _add_customer_dialog(self):
        dlg = self._dialog("إضافة عميل جديد", 450, 550)
        fields = {}
        labels = ["الاسم", "رقم الهاتف", "البريد الإلكتروني", "العنوان", "حد الائتمان", "ملاحظات"]
        types = ["عادي", "جملة", "مميز"]
        for label in labels[:-1]:
            tk.Label(dlg, text=label, font=FONT_AR, bg=CARD, fg=TEXT, anchor="w").pack(fill="x", padx=20, pady=(10,0))
            e = tk.Entry(dlg, font=FONT_AR, bg=ACCENT2, fg=TEXT, insertbackground=TEXT, relief="flat")
            e.pack(fill="x", padx=20, ipady=6)
            fields[label] = e
        tk.Label(dlg, text="نوع العميل", font=FONT_AR, bg=CARD, fg=TEXT, anchor="w").pack(fill="x", padx=20, pady=(10,0))
        type_var = tk.StringVar(value="عادي")
        type_cb = ttk.Combobox(dlg, textvariable=type_var, values=types, font=FONT_AR)
        type_cb.pack(fill="x", padx=20, ipady=4)
        tk.Label(dlg, text="ملاحظات", font=FONT_AR, bg=CARD, fg=TEXT, anchor="w").pack(fill="x", padx=20, pady=(10,0))
        notes_entry = tk.Entry(dlg, font=FONT_AR, bg=ACCENT2, fg=TEXT, insertbackground=TEXT, relief="flat")
        notes_entry.pack(fill="x", padx=20, ipady=6)
        
        def save():
            name = fields["الاسم"].get().strip()
            if not name:
                messagebox.showerror("خطأ", "الرجاء إدخال اسم العميل")
                return
            phone = fields["رقم الهاتف"].get().strip()
            try:
                credit_limit = float(fields["حد الائتمان"].get()) if fields["حد الائتمان"].get() else 0
            except ValueError:
                messagebox.showerror("خطأ", "حد الائتمان يجب أن يكون رقماً صحيحاً")
                return
            conn = get_conn()
            try:
                conn.execute("""INSERT INTO customers (name, phone, email, address, customer_type, reg_date, credit_limit, notes) 
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", 
                            (name, phone, fields["البريد الإلكتروني"].get(), fields["العنوان"].get(), 
                             type_var.get(), date.today().isoformat(), credit_limit, notes_entry.get()))
                new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                log_audit(self.username, "ADD", "customers", new_id, None, name, conn=conn)
                conn.commit()
                dlg.destroy()
                self._load_customers()
                messagebox.showinfo("تم", "تمت إضافة العميل بنجاح!")
            except Exception as e:
                conn.rollback()
                messagebox.showerror("خطأ في قاعدة البيانات", str(e))
            finally:
                pass  # singleton - لا يُغلق الاتصال
        self._btn(dlg, "💾 حفظ", save, SUCCESS).pack(pady=15)

    def _edit_customer(self):
        sel = self.cust_tree.selection()
        if not sel:
            messagebox.showwarning("تنبيه", "الرجاء تحديد عميل")
            return
        values = self.cust_tree.item(sel[0])["values"]
        customer_id = values[0]
        conn = get_conn()
        try:
            customer = conn.execute("SELECT name, phone, email, address, customer_type, credit_limit, notes FROM customers WHERE id=?", (customer_id,)).fetchone()
        finally:
            pass  # singleton - لا يُغلق الاتصال
        dlg = self._dialog("تعديل بيانات العميل", 450, 550)
        types = ["عادي", "جملة", "مميز"]
        
        tk.Label(dlg, text="الاسم", font=FONT_AR, bg=CARD, fg=TEXT, anchor="w").pack(fill="x", padx=20, pady=(10,0))
        name_entry = tk.Entry(dlg, font=FONT_AR, bg=ACCENT2, fg=TEXT, insertbackground=TEXT, relief="flat")
        name_entry.insert(0, customer[0])
        name_entry.pack(fill="x", padx=20, ipady=6)
        
        tk.Label(dlg, text="رقم الهاتف", font=FONT_AR, bg=CARD, fg=TEXT, anchor="w").pack(fill="x", padx=20, pady=(10,0))
        phone_entry = tk.Entry(dlg, font=FONT_AR, bg=ACCENT2, fg=TEXT, insertbackground=TEXT, relief="flat")
        phone_entry.insert(0, customer[1] if customer[1] else "")
        phone_entry.pack(fill="x", padx=20, ipady=6)
        
        tk.Label(dlg, text="نوع العميل", font=FONT_AR, bg=CARD, fg=TEXT, anchor="w").pack(fill="x", padx=20, pady=(10,0))
        type_var = tk.StringVar(value=customer[4] if customer[4] else "عادي")
        type_cb = ttk.Combobox(dlg, textvariable=type_var, values=types, font=FONT_AR)
        type_cb.pack(fill="x", padx=20, ipady=4)
        
        tk.Label(dlg, text="حد الائتمان", font=FONT_AR, bg=CARD, fg=TEXT, anchor="w").pack(fill="x", padx=20, pady=(10,0))
        credit_entry = tk.Entry(dlg, font=FONT_AR, bg=ACCENT2, fg=TEXT, insertbackground=TEXT, relief="flat")
        credit_entry.insert(0, str(customer[5]) if customer[5] else "0")
        credit_entry.pack(fill="x", padx=20, ipady=6)
        
        def save():
            try:
                credit_val = float(credit_entry.get()) if credit_entry.get() else 0
            except ValueError:
                messagebox.showerror("خطأ", "حد الائتمان يجب أن يكون رقماً")
                return
            conn = get_conn()
            try:
                conn.execute("UPDATE customers SET name=?, phone=?, customer_type=?, credit_limit=? WHERE id=?", 
                            (name_entry.get(), phone_entry.get(), type_var.get(), credit_val, customer_id))
                log_audit(self.username, "EDIT", "customers", customer_id, None, name_entry.get(), conn=conn)
                conn.commit()
                dlg.destroy()
                self._load_customers()
                messagebox.showinfo("تم", "تم تحديث بيانات العميل!")
            except Exception as e:
                conn.rollback()
                messagebox.showerror("خطأ في قاعدة البيانات", str(e))
            finally:
                pass  # singleton - لا يُغلق الاتصال
        self._btn(dlg, "💾 حفظ التغييرات", save, SUCCESS).pack(pady=15)

    def _delete_customer(self):
        sel = self.cust_tree.selection()
        if not sel:
            messagebox.showwarning("تنبيه", "الرجاء تحديد عميل")
            return
        values = self.cust_tree.item(sel[0])["values"]
        customer_id = values[0]
        customer_name = values[1]
        conn = get_conn()
        try:
            has_invoices = conn.execute("SELECT COUNT(*) FROM invoices WHERE customer_id=?", (customer_id,)).fetchone()[0]
        finally:
            pass  # singleton - لا يُغلق الاتصال
        if has_invoices > 0:
            messagebox.showerror("خطأ", f"لا يمكن حذف العميل '{customer_name}' لأنه لديه {has_invoices} فواتير مرتبطة")
            return
        if not messagebox.askyesno("تأكيد", f"هل تريد حذف العميل '{customer_name}'؟"):
            return
        conn = get_conn()
        try:
            conn.execute("DELETE FROM customers WHERE id=?", (customer_id,))
            log_audit(self.username, "DELETE", "customers", customer_id, customer_name, None, conn=conn)
            conn.commit()
            self._load_customers()
            messagebox.showinfo("تم", "تم حذف العميل بنجاح!")
        except Exception as e:
            conn.rollback()
            messagebox.showerror("خطأ في قاعدة البيانات", str(e))
        finally:
            pass  # singleton - لا يُغلق الاتصال

    def _show_customer_account(self):
        sel = self.cust_tree.selection()
        if not sel:
            messagebox.showwarning("تنبيه", "الرجاء تحديد عميل")
            return
        values = self.cust_tree.item(sel[0])["values"]
        customer_id = values[0]
        customer_name = values[1]
        dlg = self._dialog(f"حساب العميل: {customer_name}", 1000, 500)
        
        balance = get_customer_balance(customer_id)
        conn = get_conn()
        balance_frame = tk.Frame(dlg, bg=CARD)
        balance_frame.pack(fill="x", padx=20, pady=10)
        if balance < 0:
            balance_text = f"الرصيد الحالي: {abs(balance):,.0f} {CURRENCY_SYP} (مدين عليه)"
            balance_color = DANGER
        else:
            balance_text = f"الرصيد الحالي: {balance:,.0f} {CURRENCY_SYP} (دائن له)"
            balance_color = SUCCESS
        tk.Label(balance_frame, text=balance_text, font=FONT_BIG, bg=CARD, fg=balance_color).pack()
        
        tk.Label(dlg, text="📄 فواتير العميل", font=FONT_BIG, bg=CARD, fg=ACCENT).pack(pady=10)
        frame = tk.Frame(dlg, bg=CARD)
        frame.pack(fill="both", expand=True, padx=20, pady=5)
        cols = ("رقم الفاتورة", "التاريخ", "تاريخ الاستحقاق", "نوع الدفع", "الإجمالي", "المدفوع", "المتبقي", "الحالة")
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=6)
        for col in cols:
            tree.heading(col, text=col)
            tree.column(col, width=110, anchor="center")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        
        try:
            invoices = conn.execute("""SELECT invoice_number, invoice_date, due_date, payment_type, total_syp, paid_syp, 
                                     (total_syp - paid_syp), status FROM invoices WHERE customer_id=? ORDER BY invoice_date DESC""", 
                                   (customer_id,)).fetchall()
            for inv in invoices:
                values = (inv[0], inv[1], inv[2] or "-", inv[3], f"{inv[4]:,.0f}", f"{inv[5]:,.0f}", f"{inv[6]:,.0f}", inv[7])
                tree.insert("", "end", values=values)
        finally:
            pass  # singleton - لا يُغلق الاتصال
        self._btn(dlg, "إغلاق", dlg.destroy, ACCENT).pack(pady=15)
        
    # =====================================================
    #   صفحة الموردين
    # =====================================================
    def page_suppliers(self):
        self._page_header("🏭 إدارة الموردين", self._add_supplier_dialog if self.has_permission('edit_customers') else None)
        
        search_frame = tk.Frame(self.content, bg=BG)
        search_frame.pack(fill="x", padx=20, pady=5)
        tk.Label(search_frame, text="بحث:", bg=BG, fg=TEXT).pack(side="left", padx=5)
        self.supplier_search_entry = tk.Entry(search_frame, font=FONT_AR, bg=ACCENT2, fg=TEXT, width=25)
        self.supplier_search_entry.pack(side="left", padx=5)
        self.supplier_search_entry.bind("<KeyRelease>", lambda e: self._load_suppliers())
        self._btn(search_frame, "بحث", self._load_suppliers, ACCENT).pack(side="left", padx=5)
        
        frame = tk.Frame(self.content, bg=BG)
        frame.pack(fill="both", expand=True, padx=20, pady=5)
        cols = ("الرقم", "الاسم", "الهاتف", "البريد", "العنوان", "تاريخ التسجيل")
        self.supp_tree = ttk.Treeview(frame, columns=cols, show="headings")
        for col in cols:
            self.supp_tree.heading(col, text=col)
            self.supp_tree.column(col, width=150, anchor="center")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.supp_tree.yview)
        self.supp_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.supp_tree.pack(fill="both", expand=True)
        
        btn_frame = tk.Frame(self.content, bg=BG)
        btn_frame.pack(pady=8)
        if self.has_permission('edit_customers'):
            self._btn(btn_frame, "✏️ تعديل", self._edit_supplier, WARNING).pack(side="right", padx=5)
            self._btn(btn_frame, "🗑️ حذف", self._delete_supplier, DANGER).pack(side="right", padx=5)
        self._btn(btn_frame, "📊 حساب المورد", self._show_supplier_account, ACCENT).pack(side="right", padx=5)
        self._load_suppliers()

    def _load_suppliers(self):
        self.supp_tree.delete(*self.supp_tree.get_children())
        keyword = self.supplier_search_entry.get().strip()
        conn = get_conn()
        try:
            if keyword:
                rows = conn.execute("SELECT id, name, phone, email, address, reg_date FROM suppliers WHERE name LIKE ? OR phone LIKE ?", 
                                   (f'%{keyword}%', f'%{keyword}%')).fetchall()
            else:
                rows = conn.execute("SELECT id, name, phone, email, address, reg_date FROM suppliers").fetchall()
        finally:
            pass  # singleton - لا يُغلق الاتصال
        for row in rows:
            self.supp_tree.insert("", "end", values=row)

    def _add_supplier_dialog(self):
        dlg = self._dialog("إضافة مورد جديد", 450, 500)
        fields = {}
        labels = ["الاسم", "رقم الهاتف", "البريد الإلكتروني", "العنوان", "ملاحظات"]
        for label in labels:
            tk.Label(dlg, text=label, font=FONT_AR, bg=CARD, fg=TEXT, anchor="w").pack(fill="x", padx=20, pady=(10,0))
            e = tk.Entry(dlg, font=FONT_AR, bg=ACCENT2, fg=TEXT, insertbackground=TEXT, relief="flat")
            e.pack(fill="x", padx=20, ipady=6)
            fields[label] = e
        
        def save():
            name = fields["الاسم"].get().strip()
            if not name:
                messagebox.showerror("خطأ", "الرجاء إدخال اسم المورد")
                return
            conn = get_conn()
            try:
                conn.execute("""INSERT INTO suppliers (name, phone, email, address, reg_date, notes) 
                               VALUES (?, ?, ?, ?, ?, ?)""", 
                            (name, fields["رقم الهاتف"].get(), fields["البريد الإلكتروني"].get(),
                             fields["العنوان"].get(), date.today().isoformat(), fields["ملاحظات"].get()))
                conn.commit()
                dlg.destroy()
                self._load_suppliers()
                messagebox.showinfo("تم", "تمت إضافة المورد بنجاح!")
            except Exception as e:
                conn.rollback()
                messagebox.showerror("خطأ في قاعدة البيانات", str(e))
            finally:
                pass  # singleton - لا يُغلق الاتصال
        self._btn(dlg, "💾 حفظ", save, SUCCESS).pack(pady=15)

    def _edit_supplier(self):
        sel = self.supp_tree.selection()
        if not sel:
            messagebox.showwarning("تنبيه", "الرجاء تحديد مورد")
            return
        values = self.supp_tree.item(sel[0])["values"]
        supplier_id = values[0]
        dlg = self._dialog("تعديل بيانات المورد", 450, 500)
        
        tk.Label(dlg, text="الاسم", font=FONT_AR, bg=CARD, fg=TEXT, anchor="w").pack(fill="x", padx=20, pady=(10,0))
        name_entry = tk.Entry(dlg, font=FONT_AR, bg=ACCENT2, fg=TEXT, insertbackground=TEXT, relief="flat")
        name_entry.insert(0, values[1])
        name_entry.pack(fill="x", padx=20, ipady=6)
        
        tk.Label(dlg, text="رقم الهاتف", font=FONT_AR, bg=CARD, fg=TEXT, anchor="w").pack(fill="x", padx=20, pady=(10,0))
        phone_entry = tk.Entry(dlg, font=FONT_AR, bg=ACCENT2, fg=TEXT, insertbackground=TEXT, relief="flat")
        phone_entry.insert(0, values[2] if values[2] else "")
        phone_entry.pack(fill="x", padx=20, ipady=6)
        
        tk.Label(dlg, text="العنوان", font=FONT_AR, bg=CARD, fg=TEXT, anchor="w").pack(fill="x", padx=20, pady=(10,0))
        address_entry = tk.Entry(dlg, font=FONT_AR, bg=ACCENT2, fg=TEXT, insertbackground=TEXT, relief="flat")
        address_entry.insert(0, values[4] if values[4] else "")
        address_entry.pack(fill="x", padx=20, ipady=6)
        
        def save():
            conn = get_conn()
            try:
                conn.execute("UPDATE suppliers SET name=?, phone=?, address=? WHERE id=?", 
                            (name_entry.get(), phone_entry.get(), address_entry.get(), supplier_id))
                conn.commit()
                dlg.destroy()
                self._load_suppliers()
                messagebox.showinfo("تم", "تم تحديث بيانات المورد!")
            except Exception as e:
                conn.rollback()
                messagebox.showerror("خطأ في قاعدة البيانات", str(e))
            finally:
                pass  # singleton - لا يُغلق الاتصال
        self._btn(dlg, "💾 حفظ التغييرات", save, SUCCESS).pack(pady=15)

    def _delete_supplier(self):
        sel = self.supp_tree.selection()
        if not sel:
            messagebox.showwarning("تنبيه", "الرجاء تحديد مورد")
            return
        if not messagebox.askyesno("تأكيد", "هل تريد حذف هذا المورد؟"):
            return
        supplier_id = self.supp_tree.item(sel[0])["values"][0]
        conn = get_conn()
        try:
            conn.execute("DELETE FROM suppliers WHERE id=?", (supplier_id,))
            conn.commit()
            self._load_suppliers()
            messagebox.showinfo("تم", "تم حذف المورد بنجاح!")
        except Exception as e:
            conn.rollback()
            messagebox.showerror("خطأ في قاعدة البيانات", str(e))
        finally:
            pass  # singleton - لا يُغلق الاتصال

    def _show_supplier_account(self):
        sel = self.supp_tree.selection()
        if not sel:
            messagebox.showwarning("تنبيه", "الرجاء تحديد مورد")
            return
        values = self.supp_tree.item(sel[0])["values"]
        supplier_id = values[0]
        supplier_name = values[1]
        dlg = self._dialog(f"حساب المورد: {supplier_name}", 1000, 500)
        
        balance = get_supplier_balance(supplier_id)
        conn = get_conn()
        balance_frame = tk.Frame(dlg, bg=CARD)
        balance_frame.pack(fill="x", padx=20, pady=10)
        if balance > 0:
            balance_text = f"الرصيد الحالي: {balance:,.0f} {CURRENCY_SYP} (علينا له)"
            balance_color = DANGER
        else:
            balance_text = f"الرصيد الحالي: {abs(balance):,.0f} {CURRENCY_SYP} (له علينا)"
            balance_color = SUCCESS
        tk.Label(balance_frame, text=balance_text, font=FONT_BIG, bg=CARD, fg=balance_color).pack()
        
        tk.Label(dlg, text="📦 مشتريات من المورد", font=FONT_BIG, bg=CARD, fg=ACCENT).pack(pady=10)
        frame = tk.Frame(dlg, bg=CARD)
        frame.pack(fill="both", expand=True, padx=20, pady=5)
        cols = ("رقم العملية", "التاريخ", "المنتج", "الكمية", "الإجمالي", "المدفوع", "المتبقي")
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=6)
        for col in cols:
            tree.heading(col, text=col)
            tree.column(col, width=120, anchor="center")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        
        try:
            purchases = conn.execute("""SELECT purchase_number, purchase_date, item_name, quantity, total_syp, 
                                       paid_amount_syp, (total_syp - paid_amount_syp) FROM purchases 
                                       WHERE supplier_id=? ORDER BY purchase_date DESC""", (supplier_id,)).fetchall()
            for pur in purchases:
                values = (pur[0], pur[1], pur[2], pur[3], f"{pur[4]:,.0f}", f"{pur[5]:,.0f}", f"{pur[6]:,.0f}")
                tree.insert("", "end", values=values)
        finally:
            pass  # singleton - لا يُغلق الاتصال
        self._btn(dlg, "إغلاق", dlg.destroy, ACCENT).pack(pady=15)

    # =====================================================
    #   صفحة المنتجات
    # =====================================================
    def page_products(self):
        self._page_header("🛠️ إدارة المنتجات", self._add_product_dialog if self.has_permission('edit_products') else None)
        
        frame = tk.Frame(self.content, bg=BG)
        frame.pack(fill="both", expand=True, padx=20, pady=5)
        cols = ("الرقم", "المنتج", "النوع", "السعر (ليرة)", "السعر (دولار)", "تكلفة (ليرة)", "الوحدة", "الموقع", "الحالة")
        self.prod_tree = ttk.Treeview(frame, columns=cols, show="headings")
        for col in cols:
            self.prod_tree.heading(col, text=col)
            self.prod_tree.column(col, width=110, anchor="center")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.prod_tree.yview)
        self.prod_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.prod_tree.pack(fill="both", expand=True)
        self._load_products()

    def _load_products(self):
        self.prod_tree.delete(*self.prod_tree.get_children())
        conn = get_conn()
        try:
            rows = conn.execute("""SELECT id, name, type, price_syp, price_usd, cost_syp, unit, location, 
                                  CASE WHEN active=1 THEN 'نشط' ELSE 'غير نشط' END FROM products""").fetchall()
        finally:
            pass  # singleton - لا يُغلق الاتصال
        for row in rows:
            values = list(row)
            values[3] = f"{values[3]:,.0f}"
            values[4] = f"{values[4]:,.2f}"
            values[5] = f"{values[5]:,.0f}" if values[5] else "0"
            self.prod_tree.insert("", "end", values=values)

    def _add_product_dialog(self):
        dlg = self._dialog("إضافة منتج جديد", 500, 600)
        fields = {}
        labels = ["اسم المنتج", "النوع", "الوصف", "سعر البيع (ليرة)", "سعر البيع (دولار)", 
                  "تكلفة الإنتاج (ليرة)", "تكلفة الإنتاج (دولار)", "الوحدة", "موقع التخزين"]
        for label in labels:
            tk.Label(dlg, text=label, font=FONT_AR, bg=CARD, fg=TEXT, anchor="w").pack(fill="x", padx=20, pady=(8,0))
            e = tk.Entry(dlg, font=FONT_AR, bg=ACCENT2, fg=TEXT, insertbackground=TEXT, relief="flat")
            e.pack(fill="x", padx=20, ipady=5)
            fields[label] = e
        
        def save():
            name = fields["اسم المنتج"].get().strip()
            if not name:
                messagebox.showerror("خطأ", "الرجاء إدخال اسم المنتج")
                return
            try:
                price_syp = float(fields["سعر البيع (ليرة)"].get())
                price_usd = float(fields["سعر البيع (دولار)"].get())
                cost_syp = float(fields["تكلفة الإنتاج (ليرة)"].get()) if fields["تكلفة الإنتاج (ليرة)"].get() else 0
                cost_usd = float(fields["تكلفة الإنتاج (دولار)"].get()) if fields["تكلفة الإنتاج (دولار)"].get() else 0
            except:
                messagebox.showerror("خطأ", "الرجاء إدخال أسعار صحيحة")
                return
            conn = get_conn()
            try:
                conn.execute("""INSERT INTO products (name, type, description, price_syp, price_usd, cost_syp, cost_usd, unit, location) 
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", 
                            (name, fields["النوع"].get(), fields["الوصف"].get(), price_syp, price_usd, 
                             cost_syp, cost_usd, fields["الوحدة"].get(), fields["موقع التخزين"].get()))
                conn.commit()
                dlg.destroy()
                self._load_products()
                messagebox.showinfo("تم", "تمت إضافة المنتج بنجاح!")
            except Exception as e:
                conn.rollback()
                messagebox.showerror("خطأ في قاعدة البيانات", str(e))
            finally:
                pass  # singleton - لا يُغلق الاتصال
        self._btn(dlg, "💾 حفظ", save, SUCCESS).pack(pady=15)

    # =====================================================
    #   صفحة الفواتير
    # =====================================================
    def page_invoices(self):
        self._page_header("📄 إدارة الفواتير", self._add_invoice_dialog if self.has_permission('edit_invoices') else None)
        
        frame = tk.Frame(self.content, bg=BG)
        frame.pack(fill="both", expand=True, padx=20, pady=5)
        cols = ("الرقم", "رقم الفاتورة", "العميل", "التاريخ", "تاريخ الاستحقاق", "نوع الدفع", 
                "الإجمالي", "المدفوع", "المتبقي", "الحالة")
        self.inv_tree = ttk.Treeview(frame, columns=cols, show="headings")
        for col in cols:
            self.inv_tree.heading(col, text=col)
            self.inv_tree.column(col, width=105, anchor="center")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.inv_tree.yview)
        self.inv_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.inv_tree.pack(fill="both", expand=True)
        
        btn_frame = tk.Frame(self.content, bg=BG)
        btn_frame.pack(pady=8)
        if self.has_permission('edit_invoices'):
            self._btn(btn_frame, "✏️ تغيير الحالة", self._change_invoice_status, WARNING).pack(side="right", padx=5)
            self._btn(btn_frame, "💰 إضافة دفعة", self._add_payment_to_invoice, SUCCESS).pack(side="right", padx=5)
        if self.has_permission('delete_invoices'):
            self._btn(btn_frame, "🗑️ حذف", self._delete_invoice, DANGER).pack(side="right", padx=5)
        self._btn(btn_frame, "🖨️ طباعة", self._print_invoice, ACCENT).pack(side="right", padx=5)
        self._load_invoices()

    def _load_invoices(self):
        self.inv_tree.delete(*self.inv_tree.get_children())
        conn = get_conn()
        try:
            rows = conn.execute("""SELECT i.id, i.invoice_number, c.name, i.invoice_date, i.due_date,
                                  i.payment_type, i.total_syp, i.paid_syp, (i.total_syp - i.paid_syp), i.status
                                  FROM invoices i LEFT JOIN customers c ON i.customer_id = c.id ORDER BY i.id DESC""").fetchall()
        finally:
            pass  # singleton - لا يُغلق الاتصال
        for row in rows:
            values = list(row)
            values[6] = f"{values[6]:,.0f} {CURRENCY_SYP}"
            values[7] = f"{values[7]:,.0f} {CURRENCY_SYP}"
            values[8] = f"{values[8]:,.0f} {CURRENCY_SYP}"
            self.inv_tree.insert("", "end", values=values)

    def _add_invoice_dialog(self):
        dlg = self._dialog("إضافة فاتورة جديدة", 600, 650)
        conn = get_conn()
        try:
            customers = conn.execute("SELECT id, name FROM customers").fetchall()
            products = conn.execute("SELECT id, name, price_syp FROM products WHERE active=1").fetchall()
        finally:
            pass  # singleton - لا يُغلق الاتصال
        
        tk.Label(dlg, text="العميل", font=FONT_AR, bg=CARD, fg=TEXT, anchor="w").pack(fill="x", padx=20, pady=(10,0))
        cust_var = tk.StringVar()
        cust_map = {f"{r[1]}": r[0] for r in customers}
        cust_cb = ttk.Combobox(dlg, textvariable=cust_var, values=list(cust_map.keys()), font=FONT_AR)
        cust_cb.pack(fill="x", padx=20, ipady=4)
        
        tk.Label(dlg, text="نوع الدفع", font=FONT_AR, bg=CARD, fg=TEXT, anchor="w").pack(fill="x", padx=20, pady=(10,0))
        payment_type_var = tk.StringVar(value="نقدي")
        payment_frame = tk.Frame(dlg, bg=CARD)
        payment_frame.pack(fill="x", padx=20, pady=5)
        tk.Radiobutton(payment_frame, text="نقدي", variable=payment_type_var, value="نقدي", bg=CARD, fg=TEXT, selectcolor=CARD).pack(side="left", padx=10)
        tk.Radiobutton(payment_frame, text="آجل", variable=payment_type_var, value="آجل", bg=CARD, fg=TEXT, selectcolor=CARD).pack(side="left", padx=10)
        
        tk.Label(dlg, text="تاريخ الاستحقاق (للفواتير الآجلة)", font=FONT_AR, bg=CARD, fg=TEXT, anchor="w").pack(fill="x", padx=20, pady=(10,0))
        due_entry = tk.Entry(dlg, font=FONT_AR, bg=ACCENT2, fg=TEXT, insertbackground=TEXT, relief="flat")
        due_entry.insert(0, (date.today() + timedelta(days=30)).isoformat())
        due_entry.pack(fill="x", padx=20, ipady=6)
        
        tk.Label(dlg, text="المنتجات", font=FONT_BIG, bg=CARD, fg=ACCENT).pack(pady=(15,5))
        products_frame = tk.Frame(dlg, bg=CARD)
        products_frame.pack(fill="both", expand=True, padx=20, pady=5)
        
        canvas = tk.Canvas(products_frame, bg=CARD, highlightthickness=0)
        scrollbar = ttk.Scrollbar(products_frame, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg=CARD)
        scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        product_vars = []
        for prod in products:
            frame = tk.Frame(scrollable_frame, bg=CARD)
            frame.pack(fill="x", pady=2)
            var = tk.BooleanVar()
            chk = tk.Checkbutton(frame, text=f"{prod[1]} - {prod[2]:,.0f} {CURRENCY_SYP}", variable=var,
                                 bg=CARD, fg=TEXT, selectcolor=CARD, font=FONT_AR)
            chk.pack(side="left")
            tk.Label(frame, text="الكمية:", bg=CARD, fg=TEXT).pack(side="left", padx=5)
            qty_entry = tk.Entry(frame, width=8, font=FONT_AR, bg=ACCENT2, fg=TEXT)
            qty_entry.insert(0, "1")
            qty_entry.pack(side="left")
            product_vars.append((var, prod, qty_entry))
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        total_frame = tk.Frame(dlg, bg=CARD)
        total_frame.pack(fill="x", padx=20, pady=10)
        tk.Label(total_frame, text="الإجمالي:", font=FONT_BIG, bg=CARD, fg=ACCENT).pack(side="left", padx=10)
        total_var = tk.StringVar(value="0")
        total_label = tk.Label(total_frame, textvariable=total_var, font=FONT_BIG, bg=CARD, fg=SUCCESS)
        total_label.pack(side="left", padx=10)
        
        def update_total():
            total = 0
            for var, prod, qty_entry in product_vars:
                if var.get():
                    try:
                        qty = int(qty_entry.get())
                        total += prod[2] * qty
                    except:
                        pass
            total_var.set(f"{total:,.0f}")
        for var, prod, qty_entry in product_vars:
            qty_entry.bind("<KeyRelease>", lambda e: update_total())
            var.trace('w', lambda *args: update_total())
        
        tk.Label(dlg, text="ملاحظات", font=FONT_AR, bg=CARD, fg=TEXT, anchor="w").pack(fill="x", padx=20, pady=(10,0))
        notes_entry = tk.Entry(dlg, font=FONT_AR, bg=ACCENT2, fg=TEXT, insertbackground=TEXT, relief="flat")
        notes_entry.pack(fill="x", padx=20, ipady=6)
        
        def save():
            if not cust_var.get():
                messagebox.showerror("خطأ", "الرجاء اختيار عميل")
                return
            customer_id = cust_map[cust_var.get()]
            total = 0
            selected_products = []
            for var, prod, qty_entry in product_vars:
                if var.get():
                    try:
                        qty = int(qty_entry.get())
                        total += prod[2] * qty
                        selected_products.append((prod[0], qty, prod[2]))
                    except:
                        pass
            if not selected_products:
                messagebox.showerror("خطأ", "الرجاء اختيار منتج واحد على الأقل")
                return
            payment_type = payment_type_var.get()
            invoice_number = f"INV-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            conn = get_conn()
            try:
                cursor = conn.execute("""INSERT INTO invoices (invoice_number, customer_id, invoice_date, due_date, payment_type,
                                        total_syp, paid_syp, notes, status, created_by, created_at)
                                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", 
                                    (invoice_number, customer_id, date.today().isoformat(), 
                                     due_entry.get() if payment_type == 'آجل' else None, payment_type,
                                     total, total if payment_type == 'نقدي' else 0, notes_entry.get(),
                                     'مدفوعة' if payment_type == 'نقدي' else 'مفتوحة', self.username, datetime.now().isoformat()))
                invoice_id = cursor.lastrowid
                for prod_id, qty, price in selected_products:
                    conn.execute("INSERT INTO invoice_details (invoice_id, product_id, quantity, price_syp) VALUES (?, ?, ?, ?)", 
                                (invoice_id, prod_id, qty, price))
                if payment_type == 'نقدي':
                    box_row = conn.execute("SELECT id FROM cash_boxes WHERE currency='SYP'").fetchone()
                    if box_row:
                        box_id = box_row[0]
                        conn.execute("UPDATE cash_boxes SET balance = balance + ? WHERE id = ?", (total, box_id))
                        conn.execute("""INSERT INTO cash_movements (movement_date, box_id, movement_type, amount, 
                                       reference_type, reference_id, created_by) VALUES (?, ?, 'قبض', ?, 'فاتورة', ?, ?)""",
                                    (date.today().isoformat(), box_id, total, invoice_id, self.username))
                log_audit(self.username, "ADD", "invoices", invoice_id, None, invoice_number, conn=conn)
                conn.commit()
                messagebox.showinfo("تم", f"تمت إضافة الفاتورة رقم {invoice_number} بنجاح!")
                dlg.destroy()
                self._load_invoices()
            except Exception as e:
                conn.rollback()
                messagebox.showerror("خطأ في قاعدة البيانات", str(e))
            finally:
                pass  # singleton - لا يُغلق الاتصال
        self._btn(dlg, "💾 حفظ الفاتورة", save, SUCCESS).pack(pady=15)

    # =====================================================
    #   صفحة المستودع
    # =====================================================
    def page_warehouse(self):
        self._page_header("🏪 إدارة المستودع", self._add_material_dialog if self.has_permission('edit_warehouse') else None)

        frame = tk.Frame(self.content, bg=BG)
        frame.pack(fill="both", expand=True, padx=20, pady=5)
        cols = ("الرقم", "المادة", "النوع", "الوحدة", "الكمية", "الحد الأدنى", "سعر الشراء (ليرة)", "سعر الشراء (دولار)", "الموقع")
        self.wh_tree = ttk.Treeview(frame, columns=cols, show="headings")
        for col in cols:
            self.wh_tree.heading(col, text=col)
            self.wh_tree.column(col, width=110, anchor="center")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.wh_tree.yview)
        self.wh_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.wh_tree.pack(fill="both", expand=True)

        btn_frame = tk.Frame(self.content, bg=BG)
        btn_frame.pack(pady=8)
        if self.has_permission('edit_warehouse'):
            self._btn(btn_frame, "➕ إضافة مادة", self._add_material_dialog, SUCCESS).pack(side="right", padx=5)
            self._btn(btn_frame, "✏️ تعديل", self._edit_material, WARNING).pack(side="right", padx=5)
            self._btn(btn_frame, "🗑️ حذف", self._delete_material, DANGER).pack(side="right", padx=5)
        self._btn(btn_frame, "➕ تحديث الكمية", self._update_material_qty, ACCENT).pack(side="right", padx=5)
        self._load_warehouse()

    def _load_warehouse(self):
        self.wh_tree.delete(*self.wh_tree.get_children())
        conn = get_conn()
        try:
            rows = conn.execute("SELECT id, name, type, unit, quantity, min_limit, unit_price_syp, unit_price_usd, location FROM warehouse").fetchall()
        finally:
            pass  # singleton - لا يُغلق الاتصال
        for row in rows:
            tag = "low" if row[4] <= row[5] else ""
            values = list(row)
            values[6] = f"{values[6]:,.0f}" if values[6] else "0"
            values[7] = f"{values[7]:,.2f}" if values[7] else "0"
            self.wh_tree.insert("", "end", values=values, tags=(tag,))
        self.wh_tree.tag_configure("low", foreground=DANGER)

    def _add_material_dialog(self):
        dlg = self._dialog("إضافة مادة للمستودع", 500, 600)
        fields = {}
        labels = ["اسم المادة", "النوع", "الوحدة", "الكمية المتاحة", "الحد الأدنى", 
                  "سعر الشراء (ليرة)", "سعر الشراء (دولار)", "موقع التخزين", "ملاحظات"]
        for label in labels:
            tk.Label(dlg, text=label, font=FONT_AR, bg=CARD, fg=TEXT, anchor="w").pack(fill="x", padx=20, pady=(8,0))
            e = tk.Entry(dlg, font=FONT_AR, bg=ACCENT2, fg=TEXT, insertbackground=TEXT, relief="flat")
            e.pack(fill="x", padx=20, ipady=5)
            fields[label] = e

        def save():
            name = fields["اسم المادة"].get().strip()
            if not name:
                messagebox.showerror("خطأ", "الرجاء إدخال اسم المادة")
                return
            try:
                qty = float(fields["الكمية المتاحة"].get() or 0)
                min_qty = float(fields["الحد الأدنى"].get() or 0)
                price_syp = float(fields["سعر الشراء (ليرة)"].get() or 0)
                price_usd = float(fields["سعر الشراء (دولار)"].get() or 0)
            except:
                messagebox.showerror("خطأ", "الرجاء إدخال أرقام صحيحة")
                return
            conn = get_conn()
            try:
                conn.execute("""INSERT INTO warehouse (name, type, unit, quantity, min_limit, unit_price_syp, unit_price_usd, location, notes) 
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", 
                            (name, fields["النوع"].get(), fields["الوحدة"].get(), qty, min_qty, 
                             price_syp, price_usd, fields["موقع التخزين"].get(), fields["ملاحظات"].get()))
                conn.commit()
                dlg.destroy()
                self._load_warehouse()
                messagebox.showinfo("تم", "تمت إضافة المادة بنجاح!")
            except Exception as e:
                conn.rollback()
                messagebox.showerror("خطأ في قاعدة البيانات", str(e))
            finally:
                pass  # singleton - لا يُغلق الاتصال
        self._btn(dlg, "💾 حفظ", save, SUCCESS).pack(pady=15)

    def _edit_material(self):
        sel = self.wh_tree.selection()
        if not sel:
            messagebox.showwarning("تنبيه", "الرجاء تحديد مادة")
            return
        values = self.wh_tree.item(sel[0])["values"]
        material_id = values[0]
        dlg = self._dialog("تعديل بيانات المادة", 500, 550)

        tk.Label(dlg, text="اسم المادة", font=FONT_AR, bg=CARD, fg=TEXT, anchor="w").pack(fill="x", padx=20, pady=(10,0))
        name_entry = tk.Entry(dlg, font=FONT_AR, bg=ACCENT2, fg=TEXT, insertbackground=TEXT, relief="flat")
        name_entry.insert(0, values[1])
        name_entry.pack(fill="x", padx=20, ipady=6)

        tk.Label(dlg, text="النوع", font=FONT_AR, bg=CARD, fg=TEXT, anchor="w").pack(fill="x", padx=20, pady=(10,0))
        type_entry = tk.Entry(dlg, font=FONT_AR, bg=ACCENT2, fg=TEXT, insertbackground=TEXT, relief="flat")
        type_entry.insert(0, values[2] if values[2] else "")
        type_entry.pack(fill="x", padx=20, ipady=6)

        tk.Label(dlg, text="الوحدة", font=FONT_AR, bg=CARD, fg=TEXT, anchor="w").pack(fill="x", padx=20, pady=(10,0))
        unit_entry = tk.Entry(dlg, font=FONT_AR, bg=ACCENT2, fg=TEXT, insertbackground=TEXT, relief="flat")
        unit_entry.insert(0, values[3] if values[3] else "")
        unit_entry.pack(fill="x", padx=20, ipady=6)

        tk.Label(dlg, text="الحد الأدنى", font=FONT_AR, bg=CARD, fg=TEXT, anchor="w").pack(fill="x", padx=20, pady=(10,0))
        min_entry = tk.Entry(dlg, font=FONT_AR, bg=ACCENT2, fg=TEXT, insertbackground=TEXT, relief="flat")
        min_entry.insert(0, str(values[5]) if values[5] else "0")
        min_entry.pack(fill="x", padx=20, ipady=6)

        tk.Label(dlg, text="موقع التخزين", font=FONT_AR, bg=CARD, fg=TEXT, anchor="w").pack(fill="x", padx=20, pady=(10,0))
        location_entry = tk.Entry(dlg, font=FONT_AR, bg=ACCENT2, fg=TEXT, insertbackground=TEXT, relief="flat")
        location_entry.insert(0, values[8] if values[8] else "")
        location_entry.pack(fill="x", padx=20, ipady=6)

        def save():
            try:
                min_val = float(min_entry.get()) if min_entry.get() else 0
            except ValueError:
                messagebox.showerror("خطأ", "الحد الأدنى يجب أن يكون رقماً")
                return
            conn = get_conn()
            try:
                conn.execute("""UPDATE warehouse SET name=?, type=?, unit=?, min_limit=?, location=? WHERE id=?""", 
                            (name_entry.get(), type_entry.get(), unit_entry.get(), min_val, location_entry.get(), material_id))
                conn.commit()
                dlg.destroy()
                self._load_warehouse()
                messagebox.showinfo("تم", "تم تحديث بيانات المادة!")
            except Exception as e:
                conn.rollback()
                messagebox.showerror("خطأ في قاعدة البيانات", str(e))
            finally:
                pass  # singleton - لا يُغلق الاتصال
        self._btn(dlg, "💾 حفظ التغييرات", save, SUCCESS).pack(pady=15)

    def _update_material_qty(self):
        sel = self.wh_tree.selection()
        if not sel:
            messagebox.showwarning("تنبيه", "الرجاء تحديد مادة")
            return
        values = self.wh_tree.item(sel[0])["values"]
        material_id = values[0]
        current_qty = values[4]
        dlg = self._dialog("تحديث كمية المادة", 350, 220)

        tk.Label(dlg, text=f"المادة: {values[1]}", font=FONT_BIG, bg=CARD, fg=ACCENT).pack(pady=10)
        tk.Label(dlg, text=f"الكمية الحالية: {current_qty}", font=FONT_AR, bg=CARD, fg=TEXT).pack()
        tk.Label(dlg, text="الكمية الجديدة", font=FONT_AR, bg=CARD, fg=TEXT).pack(pady=(15,5))
        qty_entry = tk.Entry(dlg, font=FONT_AR, bg=ACCENT2, fg=TEXT, insertbackground=TEXT, relief="flat")
        qty_entry.insert(0, str(current_qty))
        qty_entry.pack(fill="x", padx=20, ipady=6)

        def save():
            try:
                new_qty = float(qty_entry.get())
            except:
                messagebox.showerror("خطأ", "الرجاء إدخال رقم صحيح")
                return
            conn = get_conn()
            try:
                conn.execute("UPDATE warehouse SET quantity=? WHERE id=?", (new_qty, material_id))
                conn.commit()
                dlg.destroy()
                self._load_warehouse()
                messagebox.showinfo("تم", "تم تحديث الكمية!")
            except Exception as e:
                conn.rollback()
                messagebox.showerror("خطأ في قاعدة البيانات", str(e))
            finally:
                pass  # singleton - لا يُغلق الاتصال
        self._btn(dlg, "💾 تحديث", save, SUCCESS).pack(pady=15)

    def _delete_material(self):
        sel = self.wh_tree.selection()
        if not sel:
            messagebox.showwarning("تنبيه", "الرجاء تحديد مادة")
            return
        if not messagebox.askyesno("تأكيد", "هل تريد حذف هذه المادة؟"):
            return
        material_id = self.wh_tree.item(sel[0])["values"][0]
        conn = get_conn()
        try:
            conn.execute("DELETE FROM warehouse WHERE id=?", (material_id,))
            conn.commit()
            self._load_warehouse()
            messagebox.showinfo("تم", "تم حذف المادة!")
        except Exception as e:
            conn.rollback()
            messagebox.showerror("خطأ في قاعدة البيانات", str(e))
        finally:
            pass  # singleton - لا يُغلق الاتصال

    # =====================================================
    #   صفحة الصناديق
    # =====================================================
    def page_cash_boxes(self):
        self._page_header("🏦 الصناديق", None)

        frame = tk.Frame(self.content, bg=BG)
        frame.pack(fill="both", expand=True, padx=20, pady=5)
        conn = get_conn()
        try:
            boxes = conn.execute("SELECT id, box_name, currency, balance FROM cash_boxes").fetchall()
            movements = conn.execute("""SELECT cm.movement_date, cb.box_name, cm.movement_type, cm.amount, 
                                       cm.reference_type, cm.notes FROM cash_movements cm 
                                       JOIN cash_boxes cb ON cm.box_id = cb.id ORDER BY cm.movement_date DESC LIMIT 50""").fetchall()
        finally:
            pass  # singleton - لا يُغلق الاتصال

        cards_frame = tk.Frame(frame, bg=BG)
        cards_frame.pack(fill="x", pady=10)
        for i, box in enumerate(boxes):
            card = tk.Frame(cards_frame, bg=CARD, relief="raised", bd=1)
            card.grid(row=0, column=i, padx=10, pady=10, sticky="nsew")
            cards_frame.columnconfigure(i, weight=1)
            tk.Label(card, text=box[1], font=FONT_BIG, bg=CARD, fg=ACCENT).pack(pady=(15, 5))
            tk.Label(card, text=f"{box[3]:,.2f} {box[2]}", font=("Arial", 20, "bold"), bg=CARD, fg=SUCCESS).pack(pady=(5, 15))

        tk.Label(frame, text="📋 آخر حركات الصندوق", font=FONT_BIG, bg=BG, fg=TEXT).pack(anchor="w", pady=(20, 10))
        tree_frame = tk.Frame(frame, bg=BG)
        tree_frame.pack(fill="both", expand=True)
        cols = ("التاريخ", "الصندوق", "النوع", "المبلغ", "المرجع", "ملاحظات")
        tree = ttk.Treeview(tree_frame, columns=cols, show="headings")
        for col in cols:
            tree.heading(col, text=col)
            tree.column(col, width=150, anchor="center")
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        for mov in movements:
            values = (mov[0], mov[1], mov[2], f"{mov[3]:,.2f}", mov[4], mov[5])
            tree.insert("", "end", values=values)

    # =====================================================
    #   صفحة التقارير
    # =====================================================
    def page_reports(self):
        self._page_header("📊 التقارير", None)

        report_frame = tk.Frame(self.content, bg=BG)
        report_frame.pack(fill="x", padx=20, pady=10)
        tk.Label(report_frame, text="اختر التقرير:", font=FONT_BIG, bg=BG, fg=TEXT).pack(side="left", padx=10)

        reports = [("الفواتير", "invoices"), ("العملاء", "customers"), ("الموردين", "suppliers"), 
                   ("المنتجات", "products"), ("المستودع", "warehouse"), ("حركة الصندوق", "cash_movement")]
        report_name_map = {r[0]: r[1] for r in reports}
        report_var = tk.StringVar(value="الفواتير")
        report_cb = ttk.Combobox(report_frame, textvariable=report_var, values=[r[0] for r in reports], font=FONT_AR, width=20)
        report_cb.pack(side="left", padx=10)
        self._btn(report_frame, "عرض", lambda: self._show_report(report_name_map.get(report_var.get(), "invoices")), SUCCESS).pack(side="left", padx=10)

        self.report_frame = tk.Frame(self.content, bg=BG)
        self.report_frame.pack(fill="both", expand=True, padx=20, pady=10)
        self._show_report("invoices")

    def _show_report(self, report_type):
        for w in self.report_frame.winfo_children():
            w.destroy()
        conn = get_conn()
        try:
            if report_type == "invoices":
                rows = conn.execute("""SELECT i.invoice_number, c.name, i.invoice_date, i.total_syp, i.status 
                                      FROM invoices i LEFT JOIN customers c ON i.customer_id = c.id 
                                      ORDER BY i.id DESC LIMIT 100""").fetchall()
                headers = ["رقم الفاتورة", "العميل", "التاريخ", "الإجمالي", "الحالة"]
                rows = [(r[0], r[1], r[2], f"{r[3]:,.0f} {CURRENCY_SYP}", r[4]) for r in rows]
            elif report_type == "customers":
                rows = conn.execute("SELECT name, phone, customer_type FROM customers").fetchall()
                headers = ["الاسم", "الهاتف", "النوع"]
            elif report_type == "suppliers":
                rows = conn.execute("SELECT name, phone, address FROM suppliers").fetchall()
                headers = ["الاسم", "الهاتف", "العنوان"]
            elif report_type == "products":
                rows = conn.execute("SELECT name, type, price_syp, price_usd FROM products WHERE active=1").fetchall()
                headers = ["المنتج", "النوع", "السعر (ليرة)", "السعر (دولار)"]
                rows = [(r[0], r[1], f"{r[2]:,.0f}", f"{r[3]:,.2f}") for r in rows]
            elif report_type == "warehouse":
                rows = conn.execute("SELECT name, type, quantity, min_limit, location FROM warehouse").fetchall()
                headers = ["المادة", "النوع", "الكمية", "الحد الأدنى", "الموقع"]
                rows = [(r[0], r[1], r[2], r[3], r[4] or "-") for r in rows]
            elif report_type == "cash_movement":
                rows = conn.execute("""SELECT movement_date, box_name, movement_type, amount, reference_type 
                                      FROM cash_movements cm JOIN cash_boxes cb ON cm.box_id = cb.id 
                                      ORDER BY movement_date DESC LIMIT 100""").fetchall()
                headers = ["التاريخ", "الصندوق", "النوع", "المبلغ", "المرجع"]
                rows = [(r[0], r[1], r[2], f"{r[3]:,.2f}", r[4]) for r in rows]
            else:
                rows, headers = [], []
        finally:
            pass  # singleton - لا يُغلق الاتصال
        if rows is not None and headers:
            self._display_report(rows, headers)

    def _display_report(self, rows, headers):
        container = tk.Frame(self.report_frame, bg=BG)
        container.pack(fill="both", expand=True)
        canvas = tk.Canvas(container, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg=BG)
        scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        for i, header in enumerate(headers):
            tk.Label(scrollable_frame, text=header, font=FONT_BIG, bg=ACCENT2, fg=TEXT,
                     padx=15, pady=8, relief="ridge").grid(row=0, column=i, sticky="nsew")
        for r, row in enumerate(rows, start=1):
            for c, value in enumerate(row):
                bg_color = CARD if r % 2 == 0 else ACCENT2
                tk.Label(scrollable_frame, text=str(value), font=FONT_AR, 
                         bg=bg_color, fg=TEXT, padx=15, pady=5, relief="flat").grid(row=r, column=c, sticky="nsew")
        for i in range(len(headers)):
            scrollable_frame.columnconfigure(i, weight=1)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    # =====================================================
    #   صفحات إضافية (قيد التطوير)
    # =====================================================
    def page_receipts(self):
        self._page_header("💰 سندات قبض", None)
        frame = tk.Frame(self.content, bg=BG)
        frame.pack(fill="both", expand=True, padx=20, pady=5)
        cols = ("الرقم", "رقم السند", "التاريخ", "العميل", "المبلغ (ليرة)", "طريقة الدفع", "ملاحظات")
        tree = ttk.Treeview(frame, columns=cols, show="headings")
        for col in cols:
            tree.heading(col, text=col)
            tree.column(col, width=140, anchor="center")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        conn = get_conn()
        try:
            rows = conn.execute("""SELECT rv.id, rv.voucher_number, rv.voucher_date, 
                                  COALESCE(c.name, '-'), rv.amount_syp, rv.payment_method, rv.notes
                                  FROM receipt_vouchers rv
                                  LEFT JOIN customers c ON rv.customer_id = c.id
                                  ORDER BY rv.id DESC""").fetchall()
        finally:
            pass  # singleton - لا يُغلق الاتصال
        for row in rows:
            values = list(row)
            values[4] = f"{values[4]:,.0f} {CURRENCY_SYP}"
            tree.insert("", "end", values=values)

    def page_payments(self):
        self._page_header("💸 سندات دفع", None)
        frame = tk.Frame(self.content, bg=BG)
        frame.pack(fill="both", expand=True, padx=20, pady=5)
        cols = ("الرقم", "رقم السند", "التاريخ", "المورد", "المبلغ (ليرة)", "طريقة الدفع", "ملاحظات")
        tree = ttk.Treeview(frame, columns=cols, show="headings")
        for col in cols:
            tree.heading(col, text=col)
            tree.column(col, width=140, anchor="center")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        conn = get_conn()
        try:
            rows = conn.execute("""SELECT pv.id, pv.voucher_number, pv.voucher_date,
                                  COALESCE(s.name, '-'), pv.amount_syp, pv.payment_method, pv.notes
                                  FROM payment_vouchers pv
                                  LEFT JOIN suppliers s ON pv.supplier_id = s.id
                                  ORDER BY pv.id DESC""").fetchall()
        finally:
            pass  # singleton - لا يُغلق الاتصال
        for row in rows:
            values = list(row)
            values[4] = f"{values[4]:,.0f} {CURRENCY_SYP}"
            tree.insert("", "end", values=values)

    def page_users(self):
        if not self.has_permission('view_users'):
            tk.Label(self.content, text="⛔ ليس لديك صلاحية لعرض هذه الصفحة", font=FONT_BIG, bg=BG, fg=DANGER).pack(pady=50)
            return
        self._page_header("👥 إدارة المستخدمين", None)
        frame = tk.Frame(self.content, bg=BG)
        frame.pack(fill="both", expand=True, padx=20, pady=5)
        cols = ("الرقم", "اسم المستخدم", "الاسم الكامل", "الحالة", "آخر دخول", "تاريخ الإنشاء")
        tree = ttk.Treeview(frame, columns=cols, show="headings")
        for col in cols:
            tree.heading(col, text=col)
            tree.column(col, width=160, anchor="center")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        conn = get_conn()
        try:
            rows = conn.execute("""SELECT id, username, full_name, 
                                  CASE WHEN is_active=1 THEN 'نشط' ELSE 'معطل' END,
                                  last_login, created_at FROM users ORDER BY id""").fetchall()
        finally:
            pass  # singleton - لا يُغلق الاتصال
        for row in rows:
            tree.insert("", "end", values=row)
        
        btn_frame = tk.Frame(self.content, bg=BG)
        btn_frame.pack(pady=8)
        if self.has_permission('edit_users'):
            def add_user():
                dlg = self._dialog("إضافة مستخدم جديد", 400, 380)
                tk.Label(dlg, text="اسم المستخدم", font=FONT_AR, bg=CARD, fg=TEXT).pack(anchor="w", padx=20, pady=(10,0))
                uname = tk.Entry(dlg, font=FONT_AR, bg=ACCENT2, fg=TEXT, insertbackground=TEXT, relief="flat")
                uname.pack(fill="x", padx=20, ipady=6)
                tk.Label(dlg, text="الاسم الكامل", font=FONT_AR, bg=CARD, fg=TEXT).pack(anchor="w", padx=20, pady=(10,0))
                fname = tk.Entry(dlg, font=FONT_AR, bg=ACCENT2, fg=TEXT, insertbackground=TEXT, relief="flat")
                fname.pack(fill="x", padx=20, ipady=6)
                tk.Label(dlg, text="كلمة المرور", font=FONT_AR, bg=CARD, fg=TEXT).pack(anchor="w", padx=20, pady=(10,0))
                pwd = tk.Entry(dlg, font=FONT_AR, bg=ACCENT2, fg=TEXT, insertbackground=TEXT, relief="flat", show="•")
                pwd.pack(fill="x", padx=20, ipady=6)
                def save_user():
                    if not uname.get().strip() or not pwd.get().strip():
                        messagebox.showerror("خطأ", "اسم المستخدم وكلمة المرور مطلوبان")
                        return
                    try:
                        conn2 = get_conn()
                        conn2.execute("INSERT INTO users (username, password, full_name, is_active, created_at) VALUES (?,?,?,1,?)",
                                     (uname.get().strip(), hash_password(pwd.get()), fname.get().strip(), datetime.now().isoformat()))
                        conn2.commit()
                        pass  # singleton - لا يُغلق الاتصال
                        dlg.destroy()
                        self.page_users()
                        messagebox.showinfo("تم", "تمت إضافة المستخدم بنجاح!")
                    except Exception as e:
                        messagebox.showerror("خطأ", str(e))
                self._btn(dlg, "💾 حفظ", save_user, SUCCESS).pack(pady=15)
            self._btn(btn_frame, "➕ إضافة مستخدم", add_user, SUCCESS).pack(side="right", padx=5)
            
            def toggle_user():
                sel = tree.selection()
                if not sel:
                    messagebox.showwarning("تنبيه", "الرجاء تحديد مستخدم")
                    return
                uid = tree.item(sel[0])["values"][0]
                uname_val = tree.item(sel[0])["values"][1]
                if uname_val == self.username:
                    messagebox.showerror("خطأ", "لا يمكن تعطيل حسابك الخاص")
                    return
                conn2 = get_conn()
                current = conn2.execute("SELECT is_active FROM users WHERE id=?", (uid,)).fetchone()[0]
                conn2.execute("UPDATE users SET is_active=? WHERE id=?", (0 if current else 1, uid))
                conn2.commit()
                pass  # singleton - لا يُغلق الاتصال
                self.page_users()
            self._btn(btn_frame, "🔄 تفعيل/تعطيل", toggle_user, WARNING).pack(side="right", padx=5)

    def page_audit_log(self):
        self._page_header("📋 سجل التدقيق", None)
        frame = tk.Frame(self.content, bg=BG)
        frame.pack(fill="both", expand=True, padx=20, pady=5)
        cols = ("التاريخ", "المستخدم", "العملية", "الجدول", "رقم السجل")
        log_tree = ttk.Treeview(frame, columns=cols, show="headings")
        for col in cols:
            log_tree.heading(col, text=col)
            log_tree.column(col, width=180, anchor="center")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=log_tree.yview)
        log_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        log_tree.pack(fill="both", expand=True)

        conn = get_conn()
        try:
            logs = conn.execute("SELECT timestamp, user, action, table_name, record_id FROM audit_log ORDER BY id DESC LIMIT 100").fetchall()
        finally:
            pass  # singleton - لا يُغلق الاتصال
        for log in logs:
            log_tree.insert("", "end", values=log)

    # =====================================================
    #   أدوات مساعدة
    # =====================================================
    def _add_payment_to_invoice(self):
        sel = self.inv_tree.selection()
        if not sel:
            messagebox.showwarning("تنبيه", "الرجاء تحديد فاتورة")
            return
        values = self.inv_tree.item(sel[0])["values"]
        invoice_id = values[0]
        invoice_number = values[1]
        remaining = float(values[8].replace(f" {CURRENCY_SYP}", "").replace(",", ""))
        if remaining <= 0:
            messagebox.showinfo("تنبيه", "هذه الفاتورة مدفوعة بالكامل!")
            return
        dlg = self._dialog("إضافة دفعة", 400, 300)
        tk.Label(dlg, text=f"الفاتورة: {invoice_number}", font=FONT_BIG, bg=CARD, fg=ACCENT).pack(pady=10)
        tk.Label(dlg, text=f"المبلغ المتبقي: {remaining:,.0f} {CURRENCY_SYP}", font=FONT_AR, bg=CARD, fg=WARNING).pack()
        tk.Label(dlg, text="المبلغ المراد دفعه", font=FONT_AR, bg=CARD, fg=TEXT).pack(pady=(15,5))
        amount_entry = tk.Entry(dlg, font=FONT_AR, bg=ACCENT2, fg=TEXT, insertbackground=TEXT, relief="flat")
        amount_entry.insert(0, str(remaining))
        amount_entry.pack(fill="x", padx=20, ipady=6)
        tk.Label(dlg, text="طريقة الدفع", font=FONT_AR, bg=CARD, fg=TEXT).pack(pady=(10,5))
        method_var = tk.StringVar(value="نقدي")
        method_cb = ttk.Combobox(dlg, textvariable=method_var, values=["نقدي", "تحويل بنكي", "شيك"], font=FONT_AR)
        method_cb.pack(fill="x", padx=20, ipady=4)

        def save():
            try:
                amount = float(amount_entry.get())
                if amount <= 0:
                    messagebox.showerror("خطأ", "المبلغ يجب أن يكون أكبر من صفر")
                    return
                if amount > remaining:
                    if not messagebox.askyesno("تنبيه", f"المبلغ {amount:,.0f} أكبر من المتبقي {remaining:,.0f}\nهل تريد المتابعة؟"):
                        return
            except:
                messagebox.showerror("خطأ", "الرجاء إدخال مبلغ صحيح")
                return
            voucher_number = f"REC-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            conn = get_conn()
            try:
                conn.execute("""INSERT INTO receipt_vouchers (voucher_number, voucher_date, customer_id, amount_syp, 
                              payment_method, reference_invoice_id, notes, created_by, created_at) 
                              SELECT ?, ?, customer_id, ?, ?, ?, ?, ?, ? FROM invoices WHERE id = ?""",
                            (voucher_number, date.today().isoformat(), amount, method_var.get(), invoice_id, 
                             f"دفعة للفاتورة {invoice_number}", self.username, datetime.now().isoformat(), invoice_id))
                conn.execute("""UPDATE invoices SET paid_syp = paid_syp + ?, 
                              status = CASE WHEN (total_syp - paid_syp - ?) <= 0 THEN 'مدفوعة' ELSE status END 
                              WHERE id = ?""", (amount, amount, invoice_id))
                box_row = conn.execute("SELECT id FROM cash_boxes WHERE currency='SYP'").fetchone()
                if box_row:
                    box_id = box_row[0]
                    conn.execute("UPDATE cash_boxes SET balance = balance + ? WHERE id = ?", (amount, box_id))
                    conn.execute("""INSERT INTO cash_movements (movement_date, box_id, movement_type, amount, 
                                  reference_type, reference_id, created_by) VALUES (?, ?, 'قبض', ?, 'سند قبض', ?, ?)""",
                                (date.today().isoformat(), box_id, amount, invoice_id, self.username))
                log_audit(self.username, "ADD_PAYMENT", "invoices", invoice_id, None, f"Amount: {amount}", conn=conn)
                conn.commit()
                messagebox.showinfo("تم", f"تم إضافة دفعة بقيمة {amount:,.0f} {CURRENCY_SYP}")
                dlg.destroy()
                self._load_invoices()
            except Exception as e:
                conn.rollback()
                messagebox.showerror("خطأ في قاعدة البيانات", str(e))
            finally:
                pass  # singleton - لا يُغلق الاتصال
        self._btn(dlg, "💾 إضافة الدفعة", save, SUCCESS).pack(pady=15)

    def _change_invoice_status(self):
        sel = self.inv_tree.selection()
        if not sel:
            messagebox.showwarning("تنبيه", "الرجاء تحديد فاتورة")
            return
        invoice_id = self.inv_tree.item(sel[0])["values"][0]
        dlg = self._dialog("تغيير حالة الفاتورة", 350, 200)
        tk.Label(dlg, text="الحالة الجديدة", font=FONT_AR, bg=CARD, fg=TEXT).pack(pady=10)
        status_var = tk.StringVar()
        status_cb = ttk.Combobox(dlg, textvariable=status_var, values=["مفتوحة", "مدفوعة", "ملغاة"], font=FONT_AR)
        status_cb.pack(padx=20, fill="x", ipady=4)

        def save():
            if not status_var.get():
                messagebox.showwarning("تنبيه", "الرجاء اختيار الحالة")
                return
            conn = get_conn()
            try:
                old_row = conn.execute("SELECT status FROM invoices WHERE id=?", (invoice_id,)).fetchone()
                old_status = old_row[0] if old_row else None
                conn.execute("UPDATE invoices SET status=? WHERE id=?", (status_var.get(), invoice_id))
                log_audit(self.username, "CHANGE_STATUS", "invoices", invoice_id, old_status, status_var.get(), conn=conn)
                conn.commit()
                dlg.destroy()
                self._load_invoices()
                messagebox.showinfo("تم", "تم تحديث حالة الفاتورة!")
            except Exception as e:
                conn.rollback()
                messagebox.showerror("خطأ في قاعدة البيانات", str(e))
            finally:
                pass  # singleton - لا يُغلق الاتصال
        self._btn(dlg, "💾 تحديث", save, SUCCESS).pack(pady=15)

    def _delete_invoice(self):
        sel = self.inv_tree.selection()
        if not sel:
            messagebox.showwarning("تنبيه", "الرجاء تحديد فاتورة")
            return
        if not messagebox.askyesno("تأكيد", "هل تريد حذف هذه الفاتورة؟"):
            return
        invoice_id = self.inv_tree.item(sel[0])["values"][0]
        conn = get_conn()
        try:
            conn.execute("DELETE FROM invoices WHERE id=?", (invoice_id,))
            log_audit(self.username, "DELETE", "invoices", invoice_id, None, None, conn=conn)
            conn.commit()
            self._load_invoices()
            messagebox.showinfo("تم", "تم حذف الفاتورة!")
        except Exception as e:
            conn.rollback()
            messagebox.showerror("خطأ في قاعدة البيانات", str(e))
        finally:
            pass  # singleton - لا يُغلق الاتصال

    def _print_invoice(self):
        sel = self.inv_tree.selection()
        if not sel:
            messagebox.showwarning("تنبيه", "الرجاء تحديد فاتورة")
            return
        invoice_id = self.inv_tree.item(sel[0])["values"][0]
        try:
            pdf_file = generate_invoice_pdf(invoice_id)
            if pdf_file and os.path.exists(pdf_file):
                import subprocess, platform
                system = platform.system()
                if system == 'Windows':
                    os.startfile(pdf_file)
                elif system == 'Darwin':
                    subprocess.call(['open', pdf_file])
                else:
                    subprocess.call(['xdg-open', pdf_file])
                messagebox.showinfo("تم", f"تم إنشاء الفاتورة PDF بنجاح!\nالموقع: {pdf_file}")
            else:
                messagebox.showerror("خطأ", "فشل إنشاء PDF للفاتورة")
        except Exception as e:
            messagebox.showerror("خطأ", f"حدث خطأ: {str(e)}")

    def _page_header(self, title, add_cmd):
        hdr = tk.Frame(self.content, bg=BG)
        hdr.pack(fill="x", padx=20, pady=(15, 5))
        tk.Label(hdr, text=title, font=("Arial", 18, "bold"), bg=BG, fg=ACCENT).pack(side="right")
        if add_cmd:
            self._btn(hdr, "➕ إضافة جديد", add_cmd, SUCCESS).pack(side="left")

    def _btn(self, parent, text, cmd, color):
        btn = tk.Button(parent, text=text, command=cmd, font=FONT_AR, bg=color, fg="white", 
                       activebackground=BG, activeforeground=color, relief="flat", padx=15, pady=7, cursor="hand2")
        def on_enter(e):
            btn.config(font=("Arial", 11, "bold"))
        def on_leave(e):
            btn.config(font=FONT_AR)
        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)
        return btn

    def _dialog(self, title, w, h):
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.geometry(f"{w}x{h}")
        dlg.configure(bg=CARD)
        dlg.grab_set()
        tk.Label(dlg, text=title, font=FONT_BIG, bg=CARD, fg=ACCENT).pack(pady=15)
        return dlg

# =====================================================
#   تشغيل التطبيق
# =====================================================
if __name__ == "__main__":
    root = tk.Tk()
    login = LoginWindow(root)
    root.mainloop()