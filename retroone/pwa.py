"""Public PWA resources. Register before the application's catch-all static mount.

The worker caches only the offline notice and repo-owned icons, never library
HTML, player data, emulator resources or a game. No session state is consulted.
"""
import hashlib
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response


OFFLINE_ASSETS = ('offline.html', 'favicon.svg', 'icons/icon-192.png',
                  'icons/icon-512.png', 'icons/icon-maskable-512.png')
PWA_FILES = {
    'manifest.webmanifest': 'application/manifest+json',
    'offline.html': 'text/html',
    'pwa-install.js': 'text/javascript',
    'icons/icon-192.png': 'image/png',
    'icons/icon-512.png': 'image/png',
    'icons/icon-maskable-512.png': 'image/png',
}


def register_pwa(app, web_root):
    """Expose root-scoped PWA resources with revalidation and a content version."""
    root = Path(web_root).resolve()

    def resource(name):
        path = root / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise HTTPException(404, 'PWA resource not installed')
        return path

    def static_endpoint(name, media_type):
        def serve():
            return FileResponse(resource(name), media_type=media_type,
                                headers={'Cache-Control': 'no-cache, max-age=0, must-revalidate',
                                         'X-Content-Type-Options': 'nosniff'})
        return serve

    for filename, media_type in PWA_FILES.items():
        app.add_api_route('/' + filename, static_endpoint(filename, media_type), methods=['GET'],
                          include_in_schema=False, name='pwa-' + filename.replace('/', '-'))

    @app.get('/service-worker.js', include_in_schema=False)
    def service_worker():
        source = resource('service-worker.js').read_text('utf-8')
        digest = hashlib.sha256(source.encode())
        for name in OFFLINE_ASSETS:
            digest.update(name.encode())
            digest.update(resource(name).read_bytes())
        source = source.replace('__RETROONE_PWA_VERSION__', digest.hexdigest()[:20])
        return Response(source, media_type='text/javascript',
                        headers={'Cache-Control': 'no-cache, max-age=0, must-revalidate',
                                 'Service-Worker-Allowed': '/', 'X-Content-Type-Options': 'nosniff'})
