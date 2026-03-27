# LumaSuite - Device Management & Firmware Upgrade System

**Current Version: 1.0.1**

A comprehensive Flask-based backend system for managing AV equipment including encoder/decoder devices and CS31 switchers with reliable firmware upgrade capabilities.

## Features

### Core Functionality
- **Device Discovery & Management**: Scan and catalog encoder/decoder devices and CS31 switchers
- **WebSocket Integration**: Real-time bidirectional communication with devices
- **Matrix Routing**: Configure video/audio streams across device network
- **Firmware Upgrades**: Robust upload system with multi-attempt fallback
- **HDCP Management**: Configure HDCP versions for input/output
- **REST API**: Complete API for device control and monitoring
- **Web UI**: Interactive interface for device management and matrix routing

### CS31 Firmware Upgrade (Enhanced)
- **Multi-Attempt Upload**: Automatic fallback through multiple endpoint/field/content-type combinations
- **10-Minute Monitoring**: Extended timeout for devices taking up to 8 minutes per stage:
  - Sending file (up to 8 minutes)
  - Rebooting (up to 8 minutes)
  - Version confirmation
- **Status Tracking**: Real-time upgrade progression
  - `sending_file` → `updating` → `rebooting` → `confirm_version` → `success`
- **Automatic Reachability Detection**: Device unreachable = rebooting state
- **Version Validation**: Confirms new firmware version after reboot
- **Full Telemetry**: Detailed logging of each status check

## Requirements

- Python 3.8+
- Flask & Flask-CORS
- Requests
- PIL (Pillow)
- NumPy
- PyInstaller (for building standalone executable)

Install dependencies:
```bash
pip install -r requirements-build.txt
```

For reproducible release builds, use pinned dependencies:
```bash
pip install -r requirements-build-lock.txt
```

## Quick Start

### Development Server
```bash
python lumaserver.py
```
Server runs on `http://localhost:5000`

### Build Standalone Executables
```bash
python build_executable.py windows   # Windows
python build_executable.py macos     # macOS (universal binary: Intel + Apple Silicon)
python build_executable.py linux     # Linux
```

Outputs:

- `dist/Windows/LumaSuite.exe`
- `dist/macOS/LumaSuite.app` (runs on Intel x86_64 and Apple Silicon ARM64)
- `dist/Linux/LumaSuite`

Note: Build each target on its matching OS, or use GitHub Actions workflow `.github/workflows/build-cross-platform.yml`.

Reproducibility tip: use `requirements-build-lock.txt` locally and in CI to reduce artifact size/content drift between builds. See [BUILD_STANDALONE.md](BUILD_STANDALONE.md) for detailed instructions and troubleshooting.

### Using the Launcher
```bash
python app_launcher.py
```
Launches GUI with system tray integration

## API Endpoints

### Device Management
- `POST /api/scan` - Scan for devices on network
- `GET /api/units` - List all discovered devices
- `GET /api/unit/<ip>` - Get device details
- `POST /api/ping` - Check device reachability
- `GET /api/version` - Get application version

### Firmware Upgrade
- `POST /api/upgrade` - Upload and monitor firmware upgrade
  - Parameters: `ips` (list), `firmware` (filename or path)
  - Returns detailed upgrade steps and monitoring telemetry

### Matrix Routing
- `POST /api/route` - Configure video/audio stream routing
- `POST /api/unroute` - Remove routing configuration
- `GET /api/matrix` - Get current matrix state

### HDCP Management
- `POST /api/set_hdcp` - Configure HDCP version

### WebSocket
- `/ws/<device_ip>` - WebSocket endpoint for real-time device communication

## Configuration

Edit settings in `lumaserver.py`:
- `CONFIG["TIMEOUT"]` - Default request timeout (seconds)
- `CONFIG["http_port"]` - HTTP port for devices (default: 80)
- `CONFIG["password"]` - Device authentication password
- `CONFIG["UPLOAD_CHUNK_SIZE"]` - Firmware upload chunk size (bytes)
- `CONFIG["UPLOAD_LOG_INTERVAL"]` - Upload progress log interval (bytes)
- `CONFIG["FIREWARE_FILE"]` - Field name for firmware uploads (CS31-specific)

## Firmware Upgrade Example

```bash
curl -X POST http://localhost:5000/api/upgrade \
  -H "Content-Type: application/json" \
  -d '{
    "ips": ["192.168.1.100", "192.168.1.101"],
    "firmware": "at-ome-cs31_v1.2.5.bin"
  }'
```

Response includes upgrade steps and full telemetry:
```json
{
  "ok": true,
  "results": {
    "192.168.1.100": {
      "ok": true,
      "steps": [
        {"step": "precheck", "result": "ok"},
        {"step": "login", "result": "ok"},
        {"step": "switcher_detect", "result": "yes"},
        {"step": "upload_switcher", "result": "ok", "detail": "Device closed connection during write (expected)"},
        {"step": "monitor_upgrade", "result": "ok", "detail": "success (reboot=487s, version=1.2.5)", 
         "telemetry": [...]}
      ]
    }
  }
}
```

## Project Structure

```
lumaSuite/
├── lumaserver.py              # Main Flask application
├── app_launcher.py            # GUI launcher with system tray
├── producer2_routes.py        # Producer/input routing logic
├── server.py                  # Server utilities
├── build_executable.py        # PyInstaller build script
├── firmware/
│   └── upload.py              # Standalone firmware upload (fallback)
├── ui/                        # Web UI assets
│   ├── index.html
│   ├── matrix.html
│   ├── producer.html
│   └── *.js, *.css
├── dist/
│   └── Windows/
│       └── LumaSuite.exe      # Built executable
└── README.md
```

## Version Control

Repository is git-initialized and ready for GitHub:

```bash
# Check status
git status

# View commit history
git log --oneline

# Make changes and commit
git add .
git commit -m "Description of changes"
```

### Uploading to GitHub

1. Create a new repository on GitHub (don't initialize with README)
2. Add remote and push:
   ```bash
   git remote add origin https://github.com/YOUR_USERNAME/lumaSuite.git
   git branch -M main
   git push -u origin main
   ```

3. Optional: Create release from executable
   ```bash
   git tag -a v1.0.0 -m "Initial release"
   git push origin v1.0.0
   ```

## Logging

All operations are logged to console with detailed information:
- Device discovery: `[scan]` tag
- Firmware upload: `[CS31]`, `[switcher-upload]` tags
- Matrix routing: `[matrix]` tag
- WebSocket: `[ws]` tag

Enable debug logging by passing `debug: true` in API requests

## Troubleshooting

### CS31 Firmware Upgrade Hanging
- Check device is powered on and connected
- Verify network connectivity: `ping <device_ip>`
- Monitor log output for status changes (`sending file` → `rebooting`)
- Ensure 10-minute timeout is sufficient for your firmware size
- Check firewall allows TCP port 80 to device

### Device Not Found
- Ensure device is on same network subnet
- Verify device has IP assigned via DHCP
- Check password in CONFIG matches device settings
- Try manual IP entry instead of network scan

### WebSocket Connection Failed
- Verify device supports WebSocket protocol
- Check device IP and port are correct
- Ensure no firewall blocking WebSocket connections
- Check device logs for authentication errors

## Development

### Code Quality
- Follow PEP 8 conventions
- Enable debug logging: `logging.basicConfig(level=logging.DEBUG)`
- Test with multiple device types before committing

### Adding Features
1. Create feature branch: `git checkout -b feature/my-feature`
2. Make changes and test
3. Commit with descriptive message: `git commit -m "Add feature: description"`
4. Push and create pull request

## License

[Add your license here]

## Support

For issues or questions, open an issue on GitHub or contact the development team.
