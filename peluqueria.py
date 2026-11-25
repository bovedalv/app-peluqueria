import libsql
from datetime import date
import streamlit as st
import pandas as pd
from google import genai


def conectar():
    if "TURSO_URL" in st.secrets and "TURSO_TOKEN" in st.secrets:
        conn = libsql.connect(
            "replica_local.db",
            sync_url=st.secrets["TURSO_URL"],
            auth_token=st.secrets["TURSO_TOKEN"]
        )
        conn.sync()
        return conn
    return libsql.connect("peluqueria.db")

def inicializar_db():
    conn = conectar()
    c = conn.cursor()
    
    c.execute(
        "CREATE TABLE IF NOT EXISTS clientes ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "nombre TEXT NOT NULL, "
        "telefono TEXT, "
        "notas_tecnicas TEXT)"
    )
    
    c.execute(
        "CREATE TABLE IF NOT EXISTS servicios ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "nombre TEXT NOT NULL, "
        "precio REAL NOT NULL)"
    )

    c.execute(
        "CREATE TABLE IF NOT EXISTS medios_pago ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "nombre TEXT NOT NULL UNIQUE)"
    )

    c.execute(
        "CREATE TABLE IF NOT EXISTS historial_comisiones ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "medio_pago_id INTEGER NOT NULL, "
        "porcentaje REAL NOT NULL, "
        "fecha_vigencia TEXT NOT NULL, "
        "nota TEXT, "
        "FOREIGN KEY (medio_pago_id) REFERENCES medios_pago(id))"
    )
    
    c.execute(
        "CREATE TABLE IF NOT EXISTS turnos ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "cliente_id INTEGER, "
        "servicio_id INTEGER, "
        "fecha TEXT NOT NULL, "
        "hora TEXT NOT NULL, "
        "precio REAL NOT NULL, "
        "estado TEXT DEFAULT 'Pendiente', "
        "FOREIGN KEY (cliente_id) REFERENCES clientes(id), "
        "FOREIGN KEY (servicio_id) REFERENCES servicios(id))"
    )
    
    c.execute(
        "CREATE TABLE IF NOT EXISTS caja ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "fecha TEXT NOT NULL, "
        "tipo TEXT NOT NULL, "
        "concepto TEXT NOT NULL, "
        "medio_pago TEXT NOT NULL, "
        "porcentaje_aplicado REAL NOT NULL, "
        "monto_bruto REAL NOT NULL, "
        "comision REAL NOT NULL, "
        "monto_neto REAL NOT NULL)"
    )
    
    c.execute("SELECT COUNT(*) FROM servicios")
    if c.fetchone()[0] == 0:
        c.executemany("INSERT INTO servicios (nombre, precio) VALUES (?, ?)", [
            ("Corte Caballero", 450.0),
            ("Corte Dama", 600.0),
            ("Color / Tintura", 1500.0),
            ("Brushing", 500.0)
        ])

    c.execute("SELECT COUNT(*) FROM medios_pago")
    if c.fetchone()[0] == 0:
        medios_init = [
            ("Efectivo", 0.0, "Sin costo"),
            ("Transferencia", 0.0, "Sin costo"),
            ("Tarjeta Débito", 1.5, "Arancel POS + IVA"),
            ("Tarjeta Crédito", 4.5, "Arancel POS 1 pago + IVA")
        ]
        for nombre, pct, nota in medios_init:
            c.execute("INSERT INTO medios_pago (nombre) VALUES (?)", (nombre,))
            m_id = c.lastrowid
            c.execute(
                "INSERT INTO historial_comisiones (medio_pago_id, porcentaje, fecha_vigencia, nota) VALUES (?, ?, ?, ?)",
                (m_id, pct, str(date.today()), nota)
            )
        for tabla in ["clientes", "servicios", "medios_pago"]:
        try:
            c.execute(f"ALTER TABLE {tabla} ADD COLUMN activo INTEGER DEFAULT 1")
        except Exception:
            pass
    
    conn.commit()
    conn.sync()
    conn.close()

def obtener_comision_vigente(conn, medio_nombre):
    query = (
        "SELECT hc.porcentaje FROM historial_comisiones hc "
        "JOIN medios_pago mp ON hc.medio_pago_id = mp.id "
        "WHERE mp.nombre = ? AND hc.fecha_vigencia <= ? "
        "ORDER BY hc.fecha_vigencia DESC, hc.id DESC LIMIT 1"
    )
    res = conn.execute(query, (medio_nombre, str(date.today()))).fetchone()
    return float(res[0]) if res else 0.0

inicializar_db()

# ==========================================
# HERRAMIENTAS DEL AGENTE DE IA (SQLite)
# ==========================================
def consultar_ficha_cliente(nombre_cliente: str) -> str:
    """Busca los datos, teléfono y notas técnicas (tinturas/fórmulas) de un cliente por su nombre."""
    conn = conectar()
    cur = conn.cursor()
    cur.execute("SELECT nombre, telefono, notas_tecnicas FROM clientes WHERE nombre LIKE ?", (f"%{nombre_cliente}%",))
    filas = cur.fetchall()
    conn.close()
    if not filas:
        return f"No encontré ningún cliente llamado '{nombre_cliente}'."
    resultado = []
    for nom, tel, notas in filas:
        resultado.append(f"Cliente: {nom} | Tel: {tel or 'Sin teléfono'} | Ficha técnica: {notas or 'Sin notas'}")
    return "\n".join(resultado)

def ver_agenda(fecha: str = "") -> str:
    """Consulta los turnos pendientes. Opcionalmente filtra por una fecha en formato YYYY-MM-DD."""
    conn = conectar()
    if fecha:
        query = (
            "SELECT t.fecha, t.hora, c.nombre, s.nombre, t.precio FROM turnos t "
            "JOIN clientes c ON t.cliente_id = c.id JOIN servicios s ON t.servicio_id = s.id "
            "WHERE t.estado = 'Pendiente' AND t.fecha = ? ORDER BY t.hora"
        )
        filas = conn.execute(query, (fecha,)).fetchall()
    else:
        query = (
            "SELECT t.fecha, t.hora, c.nombre, s.nombre, t.precio FROM turnos t "
            "JOIN clientes c ON t.cliente_id = c.id JOIN servicios s ON t.servicio_id = s.id "
            "WHERE t.estado = 'Pendiente' ORDER BY t.fecha, t.hora"
        )
        filas = conn.execute(query).fetchall()
    conn.close()
    if not filas:
        return "No hay turnos pendientes para esa consulta."
    return "\n".join([f"- {f[0]} {f[1]} hs: {f[2]} ({f[3]} - ${f[4]:.0f})" for f in filas])

def agendar_turno(nombre_cliente: str, nombre_servicio: str, fecha: str, hora: str) -> str:
    """Agenda un nuevo turno. Fecha debe ser YYYY-MM-DD y hora HH:MM. Si el cliente no existe, lo crea."""
    conn = conectar()
    cur = conn.cursor()
    
    # Buscar o crear cliente
    cur.execute("SELECT id, nombre FROM clientes WHERE nombre LIKE ?", (f"%{nombre_cliente}%",))
    cliente = cur.fetchone()
    if cliente:
        c_id, c_nom = cliente
    else:
        cur.execute("INSERT INTO clientes (nombre, telefono, notas_tecnicas) VALUES (?, '', '')", (nombre_cliente,))
        c_id = cur.lastrowid
        c_nom = nombre_cliente

    # Buscar servicio
    cur.execute("SELECT id, nombre, precio FROM servicios WHERE nombre LIKE ?", (f"%{nombre_servicio}%",))
    servicio = cur.fetchone()
    if not servicio:
        servicios_disp = [r[0] for r in cur.execute("SELECT nombre FROM servicios").fetchall()]
        conn.close()
        return f"No encontré el servicio '{nombre_servicio}'. Los servicios disponibles son: {', '.join(servicios_disp)}."
    
    s_id, s_nom, precio = servicio
    cur.execute(
        "INSERT INTO turnos (cliente_id, servicio_id, fecha, hora, precio) VALUES (?, ?, ?, ?, ?)",
        (c_id, s_id, fecha, hora, precio)
    )
    conn.commit()
    conn.sync()
    conn.close()
    return f"Turno agendado con éxito: {c_nom} para {s_nom} el {fecha} a las {hora} hs (${precio:.0f})."

def registrar_gasto(concepto: str, monto: float) -> str:
    """Registra un egreso o gasto en la caja del día de hoy."""
    conn = conectar()
    conn.execute(
        "INSERT INTO caja (fecha, tipo, concepto, medio_pago, porcentaje_aplicado, monto_bruto, comision, monto_neto) "
        "VALUES (?, 'Egreso', ?, 'Efectivo', 0.0, ?, 0.0, ?)",
        (str(date.today()), concepto, monto, monto)
    )
    conn.commit()
    conn.sync()
    conn.close()
    return f"Gasto registrado en caja: '{concepto}' por ${monto:.0f}."

# --- INTERFAZ VISUAL ---
st.set_page_config(page_title="Gestión Peluquería", page_icon="✂️", layout="centered")
def verificar_acceso():
    if st.session_state.get("autenticado", False):
        return True

    st.title("🔒 Acceso Restringido")
    with st.form("form_login"):
        usuario = st.text_input("Usuario")
        password = st.text_input("Contraseña", type="password")
        if st.form_submit_button("Ingresar", use_container_width=True):
            user_valido = st.secrets.get("USUARIO_ADMIN", "liliana")
            pass_valida = st.secrets.get("PASSWORD_ADMIN", "Sandrala1")
            if usuario == user_valido and password == pass_valida:
                st.session_state["autenticado"] = True
                st.rerun()
            else:
                st.error("❌ Usuario o contraseña incorrectos.")
    return False

if not verificar_acceso():
    st.stop()

st.title("✂️ Mi Peluquería")

tab_agenda, tab_clientes, tab_servicios, tab_caja, tab_ia, tab_abm = st.tabs([
    "📅 Agenda", "👤 Clientes", "🏷️ Tarifas", "💰 Caja", "🤖 Asistente IA", "⚙️ ABM"
])

# ==========================================
# PESTAÑA 1: AGENDA
# ==========================================
with tab_agenda:
    st.subheader("Nuevo Turno")
    conn = conectar()
    clientes_df = pd.read_sql_query("SELECT id, nombre FROM clientes WHERE COALESCE(activo, 1) = 1 ORDER BY nombre", conn)
    servicios_df = pd.read_sql_query("SELECT id, nombre, precio FROM servicios WHERE COALESCE(activo, 1) = 1 ORDER BY nombre)
    medios_df = pd.read_sql_query("SELECT id, nombre FROM medios_pago WHERE COALESCE(activo, 1) = 1)
    
    if clientes_df.empty:
        st.warning("⚠️ Primero carga al menos un cliente en la pestaña 'Clientes'.")
    else:
        with st.form("form_turno", clear_on_submit=True):
            col1, col2 = st.columns(2)
            cliente_sel = col1.selectbox("Cliente", clientes_df["nombre"])
            servicio_sel = col2.selectbox("Servicio", servicios_df["nombre"])
            
            col3, col4 = st.columns(2)
            fecha_sel = col3.date_input("Fecha", value=date.today())
            hora_sel = col4.time_input("Hora")
            
            if st.form_submit_button("➕ Agendar Turno", use_container_width=True):
                c_id = int(clientes_df[clientes_df["nombre"] == cliente_sel]["id"].values[0])
                fila_serv = servicios_df[servicios_df["nombre"] == servicio_sel].iloc[0]
                s_id = int(fila_serv["id"])
                precio_actual = float(fila_serv["precio"])
                
                c = conn.cursor()
                c.execute(
                    "INSERT INTO turnos (cliente_id, servicio_id, fecha, hora, precio) VALUES (?, ?, ?, ?, ?)",
                    (c_id, s_id, str(fecha_sel), hora_sel.strftime("%H:%M"), precio_actual)
                )
                conn.commit()
                conn.sync()
                st.success(f"Turno agendado para {cliente_sel} (${precio_actual:.0f})")
                st.rerun()

    st.divider()
    st.subheader("Turnos Pendientes")
    query_turnos = (
        "SELECT t.id, t.fecha, t.hora, c.nombre AS Cliente, s.nombre AS Servicio, t.precio AS Precio "
        "FROM turnos t "
        "JOIN clientes c ON t.cliente_id = c.id "
        "JOIN servicios s ON t.servicio_id = s.id "
        "WHERE t.estado = 'Pendiente' ORDER BY t.fecha, t.hora"
    )
    turnos_pendientes = pd.read_sql_query(query_turnos, conn)
    
    if turnos_pendientes.empty:
        st.info("No hay turnos pendientes.")
    else:
        for _, fila in turnos_pendientes.iterrows():
            with st.container(border=True):
                st.write(f"**{fila['fecha']} - {fila['hora']} hs** | 👤 **{fila['Cliente']}**")
                st.write(f"✂️ {fila['Servicio']} — **${fila['Precio']:.0f}**")
                
                col_pago1, col_pago2 = st.columns([2, 1])
                medio_sel = col_pago1.selectbox(
                    "Medio de pago", 
                    medios_df["nombre"], 
                    key=f"medio_{fila['id']}",
                    label_visibility="collapsed"
                )
                
                if col_pago2.button("✅ Cobrar", key=f"cobrar_{fila['id']}", use_container_width=True):
                    pct = obtener_comision_vigente(conn, medio_sel)
                    bruto = float(fila["Precio"])
                    comision = round(bruto * (pct / 100.0), 2)
                    neto = round(bruto - comision, 2)
                    
                    c = conn.cursor()
                    c.execute("UPDATE turnos SET estado = 'Cobrado' WHERE id = ?", (fila['id'],))
                    concepto = f"{fila['Servicio']} - {fila['Cliente']}"
                    c.execute(
                        "INSERT INTO caja (fecha, tipo, concepto, medio_pago, porcentaje_aplicado, monto_bruto, comision, monto_neto) "
                        "VALUES (?, 'Ingreso', ?, ?, ?, ?, ?, ?)",
                        (str(date.today()), concepto, medio_sel, pct, bruto, comision, neto)
                    )
                    conn.commit()
                    conn.sync()
                    st.toast(f"Cobrado con {medio_sel} ({pct}% desc. -> Neto: ${neto:.0f})")
                    st.rerun()
    conn.close()

# ==========================================
# PESTAÑA 2: CLIENTES
# ==========================================
with tab_clientes:
    st.subheader("Agregar Cliente")
    with st.form("form_cliente", clear_on_submit=True):
        nombre_c = st.text_input("Nombre y Apellido")
        tel_c = st.text_input("Teléfono / WhatsApp")
        notas_c = st.text_area("Ficha Técnica (Tinturas, fórmulas, observaciones)")
        if st.form_submit_button("💾 Guardar Cliente", use_container_width=True):
            if nombre_c.strip():
                conn = conectar()
                conn.execute("INSERT INTO clientes (nombre, telefono, notas_tecnicas) VALUES (?, ?, ?)",
                             (nombre_c.strip(), tel_c.strip(), notas_c.strip()))
                conn.commit()
                conn.sync()
                conn.close()
                st.success(f"Cliente '{nombre_c}' guardado.")
                st.rerun()
            else:
                st.error("El nombre es obligatorio.")

    st.divider()
    st.subheader("Listado de Clientes")
    conn = conectar()
    df_c = pd.read_sql_query("SELECT nombre AS Nombre, telefono AS Teléfono, notas_tecnicas AS 'Ficha Técnica' FROM clientes ORDER BY nombre", conn)
    conn.close()
    st.dataframe(df_c, use_container_width=True, hide_index=True)

# ==========================================
# PESTAÑA 3: SERVICIOS Y TABLA DE COMISIONES
# ==========================================
with tab_servicios:
    st.subheader("Agregar Servicio")
    with st.form("form_servicio", clear_on_submit=True):
        col_s1, col_s2 = st.columns(2)
        nombre_s = col_s1.text_input("Nombre del Servicio")
        precio_s = col_s2.number_input("Precio ($)", min_value=0.0, step=50.0)
        if st.form_submit_button("➕ Agregar Servicio", use_container_width=True):
            if nombre_s.strip():
                conn = conectar()
                conn.execute("INSERT INTO servicios (nombre, precio) VALUES (?, ?)", (nombre_s.strip(), precio_s))
                conn.commit()
                conn.sync()
                conn.close()
                st.success("Servicio agregado.")
                st.rerun()

    conn = conectar()
    df_s = pd.read_sql_query("SELECT nombre AS Servicio, precio AS Precio FROM servicios ORDER BY nombre", conn)
    st.dataframe(df_s, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("📈 Actualizar Comisión de Medio de Pago")
    st.caption("Cuando el banco o POS cambie sus aranceles, registra aquí el nuevo porcentaje sin perder el historial.")
    
    medios_lista = pd.read_sql_query("SELECT id, nombre FROM medios_pago", conn)
    with st.form("form_nueva_comision", clear_on_submit=True):
        c_m1, c_m2, c_m3 = st.columns(3)
        medio_elegido = c_m1.selectbox("Medio de Pago", medios_lista["nombre"])
        nuevo_porcentaje = c_m2.number_input("Nuevo %", min_value=0.0, max_value=50.0, step=0.1)
        fecha_vig = c_m3.date_input("Vigente desde", value=date.today())
        nota_com = st.text_input("Motivo / Nota (ej: Aumento arancel POS noviembre)")
        
        if st.form_submit_button("💾 Registrar Nueva Tarifa", use_container_width=True):
            m_id = int(medios_lista[medios_lista["nombre"] == medio_elegido]["id"].values[0])
            conn.execute(
                "INSERT INTO historial_comisiones (medio_pago_id, porcentaje, fecha_vigencia, nota) VALUES (?, ?, ?, ?)",
                (m_id, nuevo_porcentaje, str(fecha_vig), nota_com.strip())
            )
            conn.commit()
            conn.sync()
            st.success(f"Nueva comisión de {nuevo_porcentaje}% registrada para {medio_elegido}.")
            st.rerun()

    st.write("**Historial de Tarifas Registradas:**")
    query_hist = (
        "SELECT mp.nombre AS 'Medio de Pago', hc.porcentaje AS '% Comisión', "
        "hc.fecha_vigencia AS 'Vigente Desde', hc.nota AS Nota "
        "FROM historial_comisiones hc "
        "JOIN medios_pago mp ON hc.medio_pago_id = mp.id "
        "ORDER BY hc.fecha_vigencia DESC, hc.id DESC"
    )
    df_hist = pd.read_sql_query(query_hist, conn)
    st.dataframe(df_hist, use_container_width=True, hide_index=True)
    conn.close()

# ==========================================
# PESTAÑA 4: CAJA DIARIA
# ==========================================
with tab_caja:
    st.subheader("Registrar Movimiento Manual")
    with st.form("form_caja", clear_on_submit=True):
        col_m1, col_m2 = st.columns(2)
        tipo_m = col_m1.selectbox("Tipo", ["Egreso", "Ingreso"])
        monto_m = col_m2.number_input("Monto ($)", min_value=0.0, step=50.0)
        concepto_m = st.text_input("Concepto (ej: Compra de tinturas, Alquiler)")
        if st.form_submit_button("💸 Registrar Movimiento", use_container_width=True):
            if concepto_m.strip() and monto_m > 0:
                conn = conectar()
                conn.execute(
                    "INSERT INTO caja (fecha, tipo, concepto, medio_pago, porcentaje_aplicado, monto_bruto, comision, monto_neto) "
                    "VALUES (?, ?, ?, 'Efectivo', 0.0, ?, 0.0, ?)",
                    (str(date.today()), tipo_m, concepto_m.strip(), monto_m, monto_m)
                )
                conn.commit()
                conn.sync()
                conn.close()
                st.success("Movimiento registrado.")
                st.rerun()

    st.divider()
    st.subheader(f"Balance de Hoy ({date.today()})")
    conn = conectar()
    df_caja = pd.read_sql_query(
        "SELECT tipo AS Tipo, concepto AS Concepto, medio_pago AS Pago, "
        "porcentaje_aplicado AS '% Desc', monto_bruto AS Bruto, "
        "comision AS Comisión, monto_neto AS Neto "
        "FROM caja WHERE fecha = ?",
        conn, params=(str(date.today()),)
    )
    conn.close()
    
    if not df_caja.empty:
        ingresos_brutos = df_caja[df_caja["Tipo"] == "Ingreso"]["Bruto"].sum()
        total_comisiones = df_caja[df_caja["Tipo"] == "Ingreso"]["Comisión"].sum()
        ingresos_netos = df_caja[df_caja["Tipo"] == "Ingreso"]["Neto"].sum()
        egresos = df_caja[df_caja["Tipo"] == "Egreso"]["Neto"].sum()
        saldo_real = ingresos_netos - egresos
        
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Facturado", f"${ingresos_brutos:.0f}")
        c2.metric("Costo POS", f"-${total_comisiones:.0f}")
        c3.metric("Egresos", f"-${egresos:.0f}")
        c4.metric("Neto Real", f"${saldo_real:.0f}")
        
        st.dataframe(df_caja, use_container_width=True, hide_index=True)
    else:
        st.info("Todavía no hay movimientos en la caja hoy.")

# ==========================================
# PESTAÑA 5: ASISTENTE IA (SECRETARIO VIRTUAL)
# ==========================================
with tab_ia:
    st.subheader("🤖 Secretario Virtual")
    st.caption(f"Hoy es {date.today()}. Pídele agendar turnos, revisar fichas de tintura o anotar gastos.")
    
    if "chat_pelu" not in st.session_state:
        # ACUÉRDATE DE PEGAR AQUÍ TU CLAVE AQ. DE GOOGLE
        # cliente_ia = genai.Client(api_key="AQ.Ab8RN6J5B_5dIwR8tN5u4LGAMbpliPAW2Q2bMM_i-ME_xDdOTA")
        api_key = st.secrets.get("GEMINI_API_KEY", "SIN_CLAVE")
        cliente_ia = genai.Client(api_key=api_key)
        config_ia = {
            "tools": [consultar_ficha_cliente, ver_agenda, agendar_turno, registrar_gasto],
            "system_instruction": (
                f"Eres el asistente virtual de una peluquería. La fecha de hoy es {date.today()}. "
                "Ayuda al peluquero usando tus herramientas para consultar fichas técnicas, ver turnos, agendar citas y registrar gastos. "
                "Si te dice 'mañana' o 'el viernes', calcula la fecha exacta YYYY-MM-DD basándote en la fecha de hoy. "
                "Responde siempre de forma muy breve, directa y amable."
            )
        }
        st.session_state.chat_pelu = cliente_ia.chats.create(
            model="gemini-3.5-flash-lite",
            config=config_ia
        )
        st.session_state.mensajes_pelu = []

    for msg in st.session_state.mensajes_pelu:
        with st.chat_message(msg["rol"]):
            st.write(msg["texto"])

    if prompt_pelu := st.chat_input("Ej: ¿Qué tintura usa Ana? o Agendame a Laura mañana a las 16 para corte"):
        st.session_state.mensajes_pelu.append({"rol": "user", "texto": prompt_pelu})
        with st.chat_message("user"):
            st.write(prompt_pelu)

        with st.chat_message("assistant"):
            try:
                resp = st.session_state.chat_pelu.send_message(prompt_pelu)
                st.write(resp.text)
                st.session_state.mensajes_pelu.append({"rol": "assistant", "texto": resp.text})
            except Exception as e:
                st.error(f"Error al conectar con el asistente: {e}")

# ==========================================
# PESTAÑA 6: ADMINISTRACIÓN MAESTRA (ABM)
# ==========================================
with tab_abm:
    st.subheader("⚙️ Administración de Tablas Maestras")
    st.caption("Edita directamente sobre las grillas tipo Excel y presiona 'Guardar Cambios' debajo de cada tabla. Desmarca 'Activo' para dar de baja sin perder el historial.")

    sub_cli, sub_ser, sub_pag, sub_caj = st.tabs([
        "👤 Clientes", "✂️ Servicios", "💳 Medios de Pago", "🧾 Auditoría Caja"
    ])

    # --- 1. ABM CLIENTES ---
    with sub_cli:
        conn = conectar()
        df_cli = pd.read_sql_query(
            "SELECT id, nombre, telefono, notas_tecnicas, COALESCE(activo, 1) AS activo FROM clientes ORDER BY nombre",
            conn
        )
        df_cli["activo"] = df_cli["activo"].astype(bool)

        edit_cli = st.data_editor(
            df_cli,
            column_config={
                "id": st.column_config.NumberColumn("ID", disabled=True),
                "nombre": st.column_config.TextColumn("Nombre y Apellido", required=True),
                "telefono": st.column_config.TextColumn("Teléfono"),
                "notas_tecnicas": st.column_config.TextColumn("Ficha Técnica"),
                "activo": st.column_config.CheckboxColumn("Activo", default=True)
            },
            num_rows="dynamic",
            use_container_width=True,
            hide_index=True,
            key="editor_clientes"
        )

        if st.button("💾 Guardar Cambios en Clientes", use_container_width=True):
            c = conn.cursor()
            for _, row in edit_cli.iterrows():
                if pd.isna(row["nombre"]) or not str(row["nombre"]).strip():
                    continue
                act = 1 if row["activo"] else 0
                if pd.isna(row["id"]):
                    c.execute(
                        "INSERT INTO clientes (nombre, telefono, notas_tecnicas, activo) VALUES (?, ?, ?, ?)",
                        (str(row["nombre"]).strip(), str(row["telefono"] or ""), str(row["notas_tecnicas"] or ""), act)
                    )
                else:
                    c.execute(
                        "UPDATE clientes SET nombre=?, telefono=?, notas_tecnicas=?, activo=? WHERE id=?",
                        (str(row["nombre"]).strip(), str(row["telefono"] or ""), str(row["notas_tecnicas"] or ""), act, int(row["id"]))
                    )
            conn.commit()
            st.success("✅ Tabla de Clientes actualizada.")
            st.rerun()
        conn.close()

    # --- 2. ABM SERVICIOS ---
    with sub_ser:
        conn = conectar()
        df_ser = pd.read_sql_query(
            "SELECT id, nombre, precio, COALESCE(activo, 1) AS activo FROM servicios ORDER BY nombre",
            conn
        )
        df_ser["activo"] = df_ser["activo"].astype(bool)

        edit_ser = st.data_editor(
            df_ser,
            column_config={
                "id": st.column_config.NumberColumn("ID", disabled=True),
                "nombre": st.column_config.TextColumn("Servicio", required=True),
                "precio": st.column_config.NumberColumn("Precio ($)", min_value=0.0, step=50.0, required=True),
                "activo": st.column_config.CheckboxColumn("Activo", default=True)
            },
            num_rows="dynamic",
            use_container_width=True,
            hide_index=True,
            key="editor_servicios"
        )

        if st.button("💾 Guardar Cambios en Servicios", use_container_width=True):
            c = conn.cursor()
            for _, row in edit_ser.iterrows():
                if pd.isna(row["nombre"]) or not str(row["nombre"]).strip():
                    continue
                act = 1 if row["activo"] else 0
                precio_val = float(row["precio"]) if not pd.isna(row["precio"]) else 0.0
                if pd.isna(row["id"]):
                    c.execute(
                        "INSERT INTO servicios (nombre, precio, activo) VALUES (?, ?, ?)",
                        (str(row["nombre"]).strip(), precio_val, act)
                    )
                else:
                    c.execute(
                        "UPDATE servicios SET nombre=?, precio=?, activo=? WHERE id=?",
                        (str(row["nombre"]).strip(), precio_val, act, int(row["id"]))
                    )
            conn.commit()
            st.success("✅ Tabla de Servicios actualizada.")
            st.rerun()
        conn.close()

    # --- 3. ABM MEDIOS DE PAGO ---
    with sub_pag:
        conn = conectar()
        df_pag = pd.read_sql_query(
            "SELECT id, nombre, COALESCE(activo, 1) AS activo FROM medios_pago ORDER BY id",
            conn
        )
        df_pag["activo"] = df_pag["activo"].astype(bool)

        edit_pag = st.data_editor(
            df_pag,
            column_config={
                "id": st.column_config.NumberColumn("ID", disabled=True),
                "nombre": st.column_config.TextColumn("Medio de Pago", required=True),
                "activo": st.column_config.CheckboxColumn("Activo", default=True)
            },
            num_rows="dynamic",
            use_container_width=True,
            hide_index=True,
            key="editor_pagos"
        )

        if st.button("💾 Guardar Cambios en Medios de Pago", use_container_width=True):
            c = conn.cursor()
            for _, row in edit_pag.iterrows():
                if pd.isna(row["nombre"]) or not str(row["nombre"]).strip():
                    continue
                act = 1 if row["activo"] else 0
                if pd.isna(row["id"]):
                    c.execute("INSERT INTO medios_pago (nombre, activo) VALUES (?, ?)", (str(row["nombre"]).strip(), act))
                    m_id = c.lastrowid
                    c.execute(
                        "INSERT INTO historial_comisiones (medio_pago_id, porcentaje, fecha_vigencia, nota) VALUES (?, 0.0, ?, 'Alta inicial')",
                        (m_id, str(date.today()))
                    )
                else:
                    c.execute("UPDATE medios_pago SET nombre=?, activo=? WHERE id=?", (str(row["nombre"]).strip(), act, int(row["id"])))
            conn.commit()
            st.success("✅ Medios de Pago actualizados (recuerda ajustar su % en la pestaña Tarifas si agregaste uno nuevo).")
            st.rerun()
        conn.close()

    # --- 4. AUDITORÍA / ANULACIÓN DE CAJA ---
    with sub_caj:
        st.caption("Aquí puedes eliminar un movimiento de caja cargado por error.")
        conn = conectar()
        df_aud = pd.read_sql_query(
            "SELECT id, fecha, tipo, concepto, medio_pago, monto_bruto, monto_neto FROM caja ORDER BY id DESC LIMIT 30",
            conn
        )
        if df_aud.empty:
            st.info("No hay movimientos registrados.")
        else:
            st.dataframe(df_aud, use_container_width=True, hide_index=True)
            id_borrar = st.number_input("ID del movimiento a eliminar", min_value=1, step=1)
            if st.button("🗑️ Eliminar Movimiento de Caja", type="primary"):
                conn.execute("DELETE FROM caja WHERE id = ?", (int(id_borrar),))
                conn.commit()
                st.warning(f"Movimiento #{id_borrar} eliminado.")
                st.rerun()
        conn.close()
