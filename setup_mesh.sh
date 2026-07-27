#!/usr/bin/env bash
# ==============================================================================
# Script de Configuración de Red Ad-Hoc y Servicio Daemon (Mesh OS)
# Uso: sudo bash setup_mesh.sh <interfaz> <ip_estatica> <id_nodo>
# Ejemplo: sudo bash setup_mesh.sh wlan0 192.168.99.1 1
# ==============================================================================

# Detener el script si ocurre algún error
set -e

# Verificar permisos de superusuario
if [ "$EUID" -ne 0 ]; then
  echo "[ERROR] Este script debe ejecutarse con privilegios de root (sudo)." >&2
  exit 1
fi

# Validar parámetros de entrada
INTERFACE=${1:-""}
IP_ADDR=${2:-""}
NODE_ID=${3:-""}

if [ -z "$INTERFACE" ] || [ -z "$IP_ADDR" ] || [ -z "$NODE_ID" ]; then
  echo "Uso: sudo bash $0 <interfaz_wifi> <ip_estatica> <id_nodo>"
  echo "Ejemplo: sudo bash $0 wlan0 192.168.99.1 1"
  exit 1
fi

echo "======================================================================"
echo " Configurando Red Ad-Hoc Malla para Nodo $NODE_ID ($IP_ADDR) en $INTERFACE"
echo "======================================================================"

# 1. Detener servicios de red que puedan interferir con el modo Ad-Hoc (IBSS)
echo "[1/5] Deteniendo NetworkManager y WpaSupplicant..."
systemctl stop NetworkManager || true
systemctl stop wpa_supplicant || true

# Asegurar que la interfaz está apagada para configurarla
ip link set "$INTERFACE" down

# 2. Configurar la interfaz Wi-Fi en modo Ad-Hoc (IBSS)
echo "[2/5] Configurando interfaz $INTERFACE en modo IBSS (Ad-Hoc)..."
# Desbloquear radio por RF-Kill si está bloqueada
rfkill unblock wifi || true

# Configurar tipo de interfaz a ibss (Ad-Hoc)
iw dev "$INTERFACE" set type ibss

# Encender la interfaz
ip link set "$INTERFACE" up

# Unirse a la celda ad-hoc con el ESSID común y canal/frecuencia
# Frecuencia 2412 MHz corresponde a Canal 1 Wi-Fi. ESSID: MeshOS_AdHoc
# BSSID común opcional, ej: 02:CA:FE:CA:FE:01 (debe iniciar con bit local/unicast)
echo "Uniéndose a la red 'MeshOS_AdHoc' (2412 MHz)..."
iw dev "$INTERFACE" ibss join MeshOS_AdHoc 2412

# 3. Asignar dirección IP estática
echo "[3/5] Asignando dirección IP estática: $IP_ADDR/24..."
ip addr flush dev "$INTERFACE"
ip addr add "$IP_ADDR/24" dev "$INTERFACE"

# 4. Configuración de Firewall (UFW)
echo "[4/5] Configurando reglas de Firewall (puertos 5555, 5556, 5557)..."
if command -v ufw >/dev/null 2>&1; then
  # UDP 5555: OGMs y Beacons (malla)
  ufw allow 5555/udp comment 'Malla BATMAN Broadcast'
  # TCP 5556: Transferencia de tareas
  ufw allow 5556/tcp comment 'Malla BATMAN Tareas TCP'
  # TCP 5557: Sincronización de memoria
  ufw allow 5557/tcp comment 'Malla BATMAN Memoria TCP'
  ufw reload || true
  echo "Reglas de firewall cargadas con UFW."
elif command -v iptables >/dev/null 2>&1; then
  iptables -A INPUT -p udp --dport 5555 -j ACCEPT
  iptables -A INPUT -p tcp --dport 5556 -j ACCEPT
  iptables -A INPUT -p tcp --dport 5557 -j ACCEPT
  echo "Reglas de firewall configuradas con iptables."
else
  echo "[ADVERTENCIA] No se detectó UFW ni iptables. Asegúrese de habilitar los puertos manualmente."
fi

# 5. Crear el servicio systemd para ejecución automática en arranque
echo "[5/5] Creando servicio systemd 'mesh-node.service'..."

# Determinar ruta absoluta de ejecución
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
NODE_FILE="$SCRIPT_DIR/mesh/node.py"

if [ ! -f "$NODE_FILE" ]; then
  # Intentar buscarlo en el home por si acaso
  NODE_FILE="/home/$SUDO_USER/mesh_os/mesh/node.py"
fi

echo "Ruta de mesh/node.py detectada en: $NODE_FILE"
WORK_DIR="$(dirname "$(dirname "$NODE_FILE")")"

# Escribir el archivo del servicio unit
SERVICE_FILE="/etc/systemd/system/mesh-node.service"
cat <<EOF > "$SERVICE_FILE"
[Unit]
Description=Servicio de Nodo Ad-Hoc BATMAN (Mesh OS)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$WORK_DIR
ExecStart=/usr/bin/python3 -m mesh.node --id $NODE_ID --interface $INTERFACE --bind $IP_ADDR
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# Recargar daemon de systemd para aplicar cambios
systemctl daemon-reload

echo "----------------------------------------------------------------------"
echo " ¡Configuración completa exitosamente!"
echo " Interfaz Wi-Fi: $INTERFACE (Modo: Ad-Hoc / ESSID: MeshOS_AdHoc)"
echo " IP del nodo:    $IP_ADDR"
echo " ID del nodo:    $NODE_ID"
echo "----------------------------------------------------------------------"
echo " Para activar el inicio automático al encender:"
echo "   sudo systemctl enable mesh-node"
echo ""
echo " Para iniciar el servicio ahora mismo:"
echo "   sudo systemctl start mesh-node"
echo ""
echo " Para monitorear logs en tiempo real:"
echo "   sudo journalctl -u mesh-node -f"
echo "----------------------------------------------------------------------"
