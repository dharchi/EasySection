bl_info = {
    "name": "EasySection Pro",
    "author": "DHA (Mostafa Tarek)",
    "version": "1.0.9",
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > EasySection",
    "description": "Smart architectural sections & professional DXF export.",
    "category": "View3D",
}

import bpy
import importlib
import urllib.request
import urllib.parse
import json
import threading
import subprocess
import sys
import os
import ssl
from bpy.app.handlers import persistent

ADDON_ID = __package__
_popup_spawned = False # القفل لمنع تكرار الرسائل

# =========================================================
# نظام قراءة المكتبات المحلية (Local Libs)
# =========================================================
def register_local_libs():
    addon_dir = os.path.dirname(__file__)
    libs_dir = os.path.join(addon_dir, "libs")
    if libs_dir not in sys.path:
        sys.path.insert(0, libs_dir)
        print(f"[EasySection] Local libs added to path: {libs_dir}")

register_local_libs()

try:
    import ezdxf
    import bidi
    import arabic_reshaper
    print("[EasySection] External libraries loaded from local /libs folder.")
except ImportError as e:
    print(f"[EasySection] CRITICAL ERROR: Could not find local libraries: {e}")

from . import logic
from . import ui
importlib.reload(logic)
importlib.reload(ui)

# =========================================================
# 1. دالة التحقق
# =========================================================
def verify_gumroad(license_key, is_background=False):
    url = "https://api.gumroad.com/v2/licenses/verify"
    values = {
        "product_id": "333GJ253sWwJZ2Z8dwsqAg==", 
        "license_key": license_key,
        "increment_uses_count": "false" if is_background else "true"
    }
    try:
        data = urllib.parse.urlencode(values).encode("utf-8")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        req = urllib.request.Request(url, data=data, headers={'User-Agent': 'Mozilla/5.0'}, method='POST')
        with urllib.request.urlopen(req, timeout=10, context=ctx) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            if res_data.get("success"):
                purchase = res_data.get("purchase", {})
                if purchase.get("refunded"): return False, "Revoked"
                variant = str(purchase.get("variants", "Pro"))
                if "pro" not in variant.lower(): return False, "Lite Version"
                return True, variant
            return False, "Invalid"
    except urllib.error.HTTPError as e:
        if e.code == 404: return False, "Invalid Code"
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, str(e)

# =========================================================
# 2. نظام الفحص الخلفي الصامت
# =========================================================
_bg_status = None
_bg_done = False

def fetch_license_thread(license_key):
    global _bg_status, _bg_done
    status, _ = verify_gumroad(license_key, is_background=True)
    _bg_status = status
    _bg_done = True

def process_bg_result():
    global _bg_status, _bg_done
    if not _bg_done: return 1.0 
    
    if _bg_status is False:
        prefs = bpy.context.preferences.addons.get(ADDON_ID)
        if prefs:
            prefs.preferences.is_verified = False
            prefs.preferences.es_variant = "None"
            print("[EasySection] !!! LICENSE REVOKED BY SERVER !!!")
            try: bpy.ops.wm.save_userpref()
            except: pass
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas: area.tag_redraw()
    
    _bg_done = False
    return None 

# =========================================================
# 3. المنطق الذكي للتشغيل (The Smart Trigger)
# =========================================================
def trigger_activation_logic():
    global _popup_spawned
    
    # التأكد إن بلندر جاهز
    if not bpy.context.window_manager.windows:
        return 0.5
    
    prefs = bpy.context.preferences.addons.get(ADDON_ID)
    if not prefs: return None
    
    p = prefs.preferences
    
    # لو مش متفعلة ومفيش بوب أب مفتوحة
    if not p.is_verified and not _popup_spawned:
        _popup_spawned = True # قفل المحبس
        bpy.ops.easysection.license_popup('INVOKE_DEFAULT')
        
    # لو متفعلة، ابدأ الفحص الخلفي (مرة واحدة لكل فايل لود)
    elif p.is_verified and not _popup_spawned:
        _popup_spawned = True 
        global _bg_done
        _bg_done = False
        t = threading.Thread(target=fetch_license_thread, args=(p.es_license_key,))
        t.start()
        bpy.app.timers.register(process_bg_result, first_interval=1.0)
        
    return None

@persistent
def on_file_load(dummy):
    global _popup_spawned
    _popup_spawned = False 
    bpy.app.timers.register(trigger_activation_logic, first_interval=1.0)

# =========================================================
# 4. الأوبريتور والـ Preferences
# =========================================================
class ES_OT_LicensePopup(bpy.types.Operator):
    bl_idname = "easysection.license_popup"
    bl_label = "EasySection Activation"
    license_input: bpy.props.StringProperty(name="License Key")

    def execute(self, context):
        status, result = verify_gumroad(self.license_input)
        prefs = context.preferences.addons[ADDON_ID].preferences
        if status is True:
            prefs.is_verified = True
            prefs.es_variant = result
            prefs.es_license_key = self.license_input
            bpy.ops.wm.save_userpref()
            for window in context.window_manager.windows:
                for area in window.screen.areas: area.tag_redraw()
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, f"Failed: {result}")
            return {'CANCELLED'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        self.layout.label(text="Enter Pro License Key:")
        self.layout.prop(self, "license_input")

class EasySectionPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_ID 
    
    es_license_key: bpy.props.StringProperty(name="License Key", default="")
    is_verified: bpy.props.BoolProperty(name="Is Verified", default=False)
    es_variant: bpy.props.StringProperty(name="Variant", default="None")

    es_language: bpy.props.EnumProperty(
        name="Language",
        items=[('EN', "English", ""), ('AR', "ﺔﻴﺒﺮﻌﻟﺍ", "")],
        default='EN'
    )

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        
        # --- صندوق التفعيل (الحماية) ---
        box_lic = layout.box()
        if self.is_verified:
            row = box_lic.row()
            row.label(text=ui.get_msg(f"Status: Activated ({self.es_variant})", f"الحالة: مفعل ({self.es_variant})"), icon='CHECKMARK')
            row.operator("easysection.license_popup", text=ui.get_msg("Change Key", "تغيير الكود"), icon='GREASEPENCIL')
        else:
            col = box_lic.column(align=True)
            col.alert = True
            col.operator("easysection.license_popup", text=ui.get_msg("ACTIVATE PRO VERSION", "تفعيل نسخة الـ Pro"), icon='CHECKMARK')

        layout.separator()

        # --- صندوق اللغة ---
        box_lang = layout.box()
        row_lang = box_lang.row(align=True)
        row_lang.label(text=ui.get_msg("Select Language:", "اختر اللغة:"), icon='WORLD')
        row_lang.prop(self, "es_language", expand=True)

        layout.separator()

        # --- إعدادات الإضافة (تظهر فقط بعد التفعيل) ---
        if self.is_verified:
            box_welcome = layout.box()
            col_welcome = box_welcome.column(align=True)

            col_welcome.label(text=ui.get_msg("EasySection - Developed by DHA (Mostafa Tarek)", "تطوير :مصطفى طارق - القطاع الميسر"), icon='MOD_BOOLEAN')
            col_welcome.label(text=ui.get_msg("Advanced Architectural Section Tool: Features a smart shader-based", "أداة قطاعات معمارية متقدمة: تتميز بنظام تظليل ذكي"), icon='FILE_3D')
            
            # استرجاع الجزء الخاص بالنصوص المنسقة (split)
            row2 = col_welcome.row()
            split2 = row2.split(factor=0.045) 
            split2.label(text="") 
            split2.label(text=ui.get_msg("sectioning system, automated geometry filtering (Smart Sorting),", "نظام قطع، فلترة هندسية تلقائية )فرز ذكي(،"))
            
            row3 = col_welcome.row()
            split3 = row3.split(factor=0.045)
            split3.label(text="") 
            split3.label(text=ui.get_msg("and dedicated modifiers to refine section visibility and CAD-ready export.", "ومعدلات مخصصة لتحسين رؤية القطاع وتصدير جاهز لبرامج الكاد."))
            
            col_welcome.separator()
            
            row_links = col_welcome.row(align=True)
            row_links.operator("wm.url_open", text=ui.get_msg("Documentation", "الشرح والكتاب التعليمي"), icon='HELP').url = "https://mmansour.my.canva.site/dha-easysection"
            row_links.operator("wm.url_open", text=ui.get_msg("DHA Youtube channel", "قناتنا على اليوتيوب"), icon='PLAY').url = "https://www.youtube.com/@MostafaTarek-DHA" 
            row_links.operator("wm.url_open", text=ui.get_msg("Product page", "صفحة المنتج"), icon='URL').url = "https://archdynamo1.gumroad.com/l/easysection"
            
            layout.separator(factor=1.0)

            layout.label(text=ui.get_msg("Slicer Controls", "إعدادات السلايسر"), icon='MOD_BOOLEAN')
            box_s = layout.box()
            box_s.label(text=ui.get_msg("Performance", "الأداء"), icon='PROPERTIES')
            col_perf = box_s.column(align=True)
            col_perf.prop(scene, "dha_sync_active", text=ui.get_msg("Realtime Mode", "الوضع المباشر"), toggle=True)
            
            box_s.label(text=ui.get_msg("Wire Settings", "إعدادات الخطوط"), icon='MOD_WIREFRAME')
            col_wire = box_s.column(align=True)
            row_style = col_wire.row(align=True)
            row_style.label(text=ui.get_msg("Style", "الشكل"))
            row_style.prop(scene, "dha_wire_mode", text="")
            
            if getattr(scene, "dha_wire_mode", "") != '2':
                row_vc = col_wire.row(align=True)
                row_vc.prop(scene, "dha_wire_slider", text=ui.get_msg("Thickness", "السمك"))
                row_vc.prop(scene, "dha_wire_color", text="")

            layout.separator(factor=0.5)

            layout.label(text=ui.get_msg("Gizmo Controls", "إعدادات الجيزمو"), icon='EMPTY_ARROWS')
            box_g = layout.box()
            
            box_g.label(text=ui.get_msg("Performance", "الأداء"), icon='PROPERTIES')
            col_g_perf = box_g.column(align=True)
            col_g_perf.prop(scene, "easysection_update_interval", text=ui.get_msg("Refresh", "تحديث"))
            col_g_perf.prop(scene, "easysection_drag_sensitivity", text=ui.get_msg("Speed", "السرعة"))
            col_g_perf.prop(scene, "easysection_use_occlusion", text=ui.get_msg("Occlusion (Raycast)", "حجب الرؤية"))
            col_g_perf.prop(scene, "easysection_use_undo", text=ui.get_msg("Record Undo", "تسجيل التراجع"))
            
            box_g.label(text=ui.get_msg("Gizmo Settings", "إعدادات الجيزمو"), icon='RESTRICT_SELECT_OFF')
            row_gs = box_g.row(align=True)
            row_gs.prop(scene, "easysection_arrow_size", text=ui.get_msg("Size", "الحجم"))
            row_gs.prop(scene, "easysection_arrow_color", text="")
        
            layout.separator(factor=0.5)
            layout.label(text=ui.get_msg("Hatch Controls", "إعدادات التهشير "), icon='NODE_MATERIAL')
            
            box_h = layout.box()
            col_h = box_h.column(align=True)
            col_h.label(text=ui.get_msg("Switch Coordinates To:", "تحويل الإحداثيات إلى:"))
            row_h = col_h.row(align=True)
            row_h.prop(scene, "hatch_coord_mode", expand=True)

classes = (EasySectionPreferences, ES_OT_LicensePopup)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    
    for cls in ui.ui_classes:
        try: bpy.utils.register_class(cls)
        except: pass
    try: ui.register_properties_and_handlers()
    except: pass

    if on_file_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(on_file_load)
    
    if not bpy.app.background:
        bpy.app.timers.register(trigger_activation_logic, first_interval=1.5)

def unregister():
    if on_file_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(on_file_load)
    ui.unregister_properties_and_handlers()
    for cls in reversed(ui.ui_classes):
        try: bpy.utils.unregister_class(cls)
        except: pass
    for cls in reversed(classes):
        try: bpy.utils.unregister_class(cls)
        except: pass

if __name__ == "__main__":
    register()