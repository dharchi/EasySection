import bpy
import os
import sys
import json
from datetime import datetime
from bpy_extras.io_utils import ExportHelper

# 1. التأكد من إضافة فولدر libs الأساسي فقط
libs_path = os.path.join(os.path.dirname(__file__), "libs")
if libs_path not in sys.path: 
    sys.path.append(libs_path)

# 2. الاستدعاء الآمن للمكتبات
try:
    import arabic_reshaper
    HAS_RESHAPER = True
except ImportError as e:
    print(f"EasySection Reshaper Error: {e}")
    HAS_RESHAPER = False

try:
    from bidi.algorithm import get_display
    HAS_BIDI = True
except ImportError as e:
    print(f"EasySection Bidi Error: {e}")
    HAS_BIDI = False

import gpu
import time
import mathutils
import bmesh
from gpu_extras.batch import batch_for_shader
from bpy_extras.view3d_utils import location_3d_to_region_2d
from bpy.app.handlers import persistent

from . import logic

TARGET_NAMES = ["DHABackwardArrow", "DHADownArrow", "DHAForwardArrow", "DHALeftArrow", "DHARightArrow", "DHAUpArrow"]
VERT_INDICES = [0, 1, 2]

PROXY_NAME = "DHASlicerBox" 
TARGET_NAME = "DHASlicerProxy"      
GN_MOD_NAME = "GeometryNodes" 
REALTIME_SOCKET = "Socket_2"   
BOX_SOCKET = "Socket_3" 
WIRE_OBJ_NAME = "DHASlicerWire"
WIRE_MOD_NAME = "GeometryNodes"

CAM_ARROW_PAIRS = {
    "DHABackwardCam": "DHABackwardArrow", "DHADownCam": "DHADownArrow",
    "DHAForwardCam": "DHAForwardArrow", "DHALeftCam": "DHALeftArrow",
    "DHARightCam": "DHARightArrow", "DHAUpCam": "DHAUpArrow"
}

last_transforms = None
idle_ticks = 0
WAIT_TICKS = 6
is_undoing = False

class GizmoData:
    arrows = {} 
    needs_update = True 
    last_view_matrix = mathutils.Matrix()
    last_update_time = 0
    eval_time = 0.0
    draw_time = 0.0
    ray_time = 0.0
    is_gizmo_allowed = True

gizmo_storage = GizmoData()

# ==================================================================
# 2. الدوال المساعدة والمنطق
# ==================================================================

_translation_cache = {}

def fix_ar(text):
    if not HAS_RESHAPER: 
        return text
    reshaped_text = arabic_reshaper.reshape(text)
    if HAS_BIDI:
        return get_display(reshaped_text)
    else:
        return reshaped_text[::-1]

def get_msg(text_en, text_ar):
    prefs = bpy.context.preferences.addons[__package__].preferences
    
    if not (prefs and prefs.es_language == 'AR'):
        return text_en
        
    if text_ar in _translation_cache:
        return _translation_cache[text_ar]
    
    processed_text = fix_ar(text_ar)
    _translation_cache[text_ar] = processed_text
    
    return processed_text

def is_gizmo_allowed(context):
    wire_obj = bpy.data.objects.get(WIRE_OBJ_NAME)
    if not wire_obj: return False
    
    if not wire_obj.visible_get(): return False
    
    space = getattr(context, "space_data", None)
    if space and space.type == 'VIEW_3D' and space.local_view:
        if not wire_obj.local_view_get(space):
            return False
            
    return True

def linear_to_srgb(color):
    return [pow(c, 1/2.2) if i < 3 else c for i, c in enumerate(color)]

def get_tracked_transforms(obj):
    transforms = [obj.matrix_world.copy()]
    for mod in obj.modifiers:
        if mod.type in {'HOOK', 'LATTICE'} and mod.object:
            transforms.append(mod.object.matrix_world.copy())
    return transforms

def has_movement(current, last):
    if last is None or len(current) != len(last): return True
    for m1, m2 in zip(current, last):
        for i in range(4):
            for j in range(4):
                if abs(m1[i][j] - m2[i][j]) > 0.0001: return True
    return False

def force_viewport_update(obj, context):
    if obj: obj.update_tag()
    if context: context.view_layer.update()

def sync_evaluated_mesh():
    if is_undoing: return 
    source_obj = bpy.data.objects.get(PROXY_NAME)
    target_obj = bpy.data.objects.get(TARGET_NAME)
    if not source_obj or not target_obj: return
    bm = bmesh.new()
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        bm.from_object(source_obj, depsgraph)
        if target_obj.data is None: target_obj.data = bpy.data.meshes.new(TARGET_NAME + "_Mesh")
        bm.to_mesh(target_obj.data)
        target_obj.data.update() 
        target_obj.matrix_world = source_obj.matrix_world.copy()
        for cam_name, arrow_name in CAM_ARROW_PAIRS.items():
            cam = bpy.data.objects.get(cam_name); arrow = bpy.data.objects.get(arrow_name)
            if cam and arrow: cam.matrix_world = arrow.matrix_world.copy()
    except: pass
    finally: bm.free()

def check_for_updates():
    global last_transforms, idle_ticks
    if is_undoing: return 0.1
    try:
        if getattr(bpy.context.scene, "dha_sync_active", False): return 0.1 
        source_obj = bpy.data.objects.get(PROXY_NAME)
        if not source_obj: return 0.1
        current_transforms = get_tracked_transforms(source_obj)
        if bpy.context.mode == 'EDIT_MESH' or has_movement(current_transforms, last_transforms):
            last_transforms = current_transforms; idle_ticks = 0
        else:
            if idle_ticks == WAIT_TICKS: sync_evaluated_mesh(); idle_ticks += 1
            elif idle_ticks < WAIT_TICKS: idle_ticks += 1
    except: pass
    return 0.1

def easysection_depsgraph_callback(scene, depsgraph):
    gizmo_storage.needs_update = True

def draw_callback_px(self, context):

    if not getattr(context.scene, "easysection_is_running", False): return

    if not gizmo_storage.is_allowed: return

    scene = context.scene; rv3d = context.region_data
    if not rv3d: return
    try: shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    except: shader = gpu.shader.from_builtin('2D_UNIFORM_COLOR')
    all_fill = []; all_out = []; sel_fill = []; sel_out = []
    occ_fill = []; occ_out = []; sel_occ_fill = []; sel_occ_out = []
    size = scene.easysection_arrow_size; gap = size * 0.5
    for name, data in gizmo_storage.arrows.items():
        orig_3d = data.get('orig')
        if not orig_3d: continue
        orig_2d = location_3d_to_region_2d(context.region, rv3d, orig_3d)
        if not orig_2d: continue
        tip_2d = location_3d_to_region_2d(context.region, rv3d, orig_3d + data['z_dir'])
        if not tip_2d: continue
        dir_2d = (tip_2d - orig_2d).normalized(); right_2d = mathutils.Vector((-dir_2d.y, dir_2d.x))
        occluded = data['occ'] and scene.easysection_use_occlusion; selected = data['sel']
        for direction in [dir_2d, -dir_2d]:
            base_pt = orig_2d + (direction * gap)
            p1 = base_pt + (direction * size); p2 = base_pt + (right_2d * size * 0.7); p3 = base_pt - (right_2d * size * 0.7)
            center_2d = (p1 + p2 + p3) / 3; scale_factor = 1.15
            o1 = center_2d + (p1 - center_2d) * scale_factor; o2 = center_2d + (p2 - center_2d) * scale_factor; o3 = center_2d + (p3 - center_2d) * scale_factor
            if occluded:
                if selected: sel_occ_fill.extend([p1, p2, p3]); sel_occ_out.extend([o1, o2, o3])
                else: occ_fill.extend([p1, p2, p3]); occ_out.extend([o1, o2, o3])
            else:
                if selected: sel_fill.extend([p1, p2, p3]); sel_out.extend([o1, o2, o3])
                else: all_fill.extend([p1, p2, p3]); all_out.extend([o1, o2, o3])
    shader.bind(); gpu.state.blend_set('ALPHA')
    def draw_b(verts, color):
        if verts:
            shader.uniform_float("color", color)
            batch_for_shader(shader, 'TRIS', {"pos": verts}).draw(shader)
    draw_b(all_out + sel_out, (0.0, 0.0, 0.0, 1.0)); draw_b(occ_out + sel_occ_out, (0.0, 0.0, 0.0, 0.25))
    draw_b(all_fill, (1.0, 1.0, 1.0, 1.0)); draw_b(occ_fill, (1.0, 1.0, 1.0, 0.25))
    sel_c = linear_to_srgb(list(scene.easysection_arrow_color))
    draw_b(sel_fill, tuple(sel_c)); draw_b(sel_occ_fill, (sel_c[0], sel_c[1], sel_c[2], sel_c[3] * 0.25))
    gpu.state.blend_set('NONE')

def update_hatch_coordinates(self, context):
    mode = context.scene.hatch_coord_mode
    for mat in bpy.data.materials:
        if mat.use_nodes and mat.name.startswith("Hatch_"):
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links
            tex_coord = next((n for n in nodes if n.type == 'TEX_COORD'), None)
            
            if tex_coord:
                target_output = tex_coord.outputs.get(mode)
                source_outputs = [tex_coord.outputs.get('UV'), tex_coord.outputs.get('Camera')]
                
                if target_output:
                    for out in source_outputs:
                        if out and out != target_output:
                            for link in out.links:
                                links.new(target_output, link.to_socket)

# ==================================================================
# 3. الأوبريتورز
# ==================================================================

class EASYSECTION_OT_interactive_arrow(bpy.types.Operator):
    bl_idname = "easysection.interactive_arrow"
    bl_label = "EasySection Master"
    _handle = None; _timer = None

    def update_eval(self, context):
        depsgraph = context.evaluated_depsgraph_get()
        for name in TARGET_NAMES:
            obj = bpy.data.objects.get(name)
            if not obj: continue
            try:
                obj_eval = obj.evaluated_get(depsgraph)
                v_coords = [obj_eval.data.vertices[i].co for i in VERT_INDICES]
                local_center = sum(v_coords, mathutils.Vector()) / 3
                if name not in gizmo_storage.arrows: gizmo_storage.arrows[name] = {}
                d = gizmo_storage.arrows[name]
                d['local'] = local_center; d['orig'] = obj_eval.matrix_world @ local_center
                d['z_dir'] = (obj_eval.matrix_world.to_3x3() @ mathutils.Vector((0, 0, 1))).normalized()
                d['sel'] = obj.select_get()
                if 'occ' not in d: d['occ'] = False
            except: pass

    def update_raycast(self, context):
        depsgraph = context.evaluated_depsgraph_get()
        rv3d = context.region_data
        if not rv3d: return
        cam_loc = rv3d.view_matrix.inverted().translation
        for name, data in gizmo_storage.arrows.items():
            if 'orig' not in data: continue
            direction = data['orig'] - cam_loc; dist = direction.length
            if dist > 0.1: 
                res, _, _, _, _, _ = context.scene.ray_cast(depsgraph, cam_loc, direction.normalized(), distance=dist - 0.1)
                data['occ'] = res

    def modal(self, context, event):
        if not context.scene.easysection_is_running: self.cleanup(context); return {'FINISHED'}
        rv3d = context.region_data
        if not rv3d: return {'PASS_THROUGH'}
        current_allowed_state = is_gizmo_allowed(context)
        
        gizmo_storage.is_allowed = current_allowed_state

        if getattr(self, "_last_allowed_state", True) != current_allowed_state:
            if getattr(context, "area", None): context.area.tag_redraw()
            self._last_allowed_state = current_allowed_state
            if not current_allowed_state:
                self.is_dragging = False

        if not current_allowed_state and event.type in {'LEFTMOUSE', 'MOUSEMOVE'}:
            return {'PASS_THROUGH'}
        if event.type == 'TIMER':
            curr = time.time(); needs_redraw = False
            if gizmo_storage.needs_update:
                if curr - gizmo_storage.last_update_time >= context.scene.easysection_update_interval:
                    self.update_eval(context)
                    if context.scene.easysection_use_occlusion: self.update_raycast(context)
                    gizmo_storage.needs_update = False; gizmo_storage.last_update_time = curr; needs_redraw = True
            elif rv3d.view_matrix != gizmo_storage.last_view_matrix:
                if context.scene.easysection_use_occlusion: self.update_raycast(context)
                gizmo_storage.last_view_matrix = rv3d.view_matrix.copy(); needs_redraw = True
            if needs_redraw: context.area.tag_redraw()

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if not current_allowed_state: return {'PASS_THROUGH'}
            mouse_pos = mathutils.Vector((event.mouse_region_x, event.mouse_region_y))
            click_radius = max(context.scene.easysection_arrow_size * 3, 35.0)
            for name, data in gizmo_storage.arrows.items():
                if 'orig' not in data: continue
                orig_2d = location_3d_to_region_2d(context.region, context.region_data, data['orig'])
                if orig_2d and (mouse_pos - orig_2d).length < click_radius:
                    bpy.ops.object.select_all(action='DESELECT')
                    obj = bpy.data.objects.get(name); obj.select_set(True)
                    context.view_layer.objects.active = obj
                    self.active_obj = obj; self.is_dragging = True; self.last_mouse_pos = mouse_pos
                    for n, d in gizmo_storage.arrows.items(): d['sel'] = (n == obj.name)
                    context.area.tag_redraw(); return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            if getattr(self, "is_dragging", False):
                self.is_dragging = False
                if context.scene.easysection_use_undo: bpy.ops.ed.undo_push(message="Move Arrow")
                return {'RUNNING_MODAL'}

        if getattr(self, "is_dragging", False) and event.type == 'MOUSEMOVE':
            mouse_delta = mathutils.Vector((event.mouse_region_x, event.mouse_region_y)) - self.last_mouse_pos
            obj = self.active_obj; data = gizmo_storage.arrows.get(obj.name)
            if data:
                local_z = data['z_dir']; orig_2d = location_3d_to_region_2d(context.region, context.region_data, data['orig'])
                tip_2d = location_3d_to_region_2d(context.region, context.region_data, data['orig'] + local_z)
                if orig_2d and tip_2d:
                    move_dir = (tip_2d - orig_2d).normalized()
                    obj.location += local_z * (mouse_delta.dot(move_dir) * context.scene.easysection_drag_sensitivity)
                    data['orig'] = obj.matrix_world @ data['local']
            self.last_mouse_pos = mathutils.Vector((event.mouse_region_x, event.mouse_region_y))
            context.area.tag_redraw(); return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}

    def invoke(self, context, event):
        context.scene.easysection_is_running = True
        self._last_allowed_state = True
        for h in bpy.app.handlers.depsgraph_update_post:
            if h.__name__ == "easysection_depsgraph_callback": bpy.app.handlers.depsgraph_update_post.remove(h)
        bpy.app.handlers.depsgraph_update_post.append(easysection_depsgraph_callback)
        self._handle = bpy.types.SpaceView3D.draw_handler_add(draw_callback_px, (self, context), 'WINDOW', 'POST_PIXEL')
        self._timer = context.window_manager.event_timer_add(0.01, window=context.window)
        context.window_manager.modal_handler_add(self); return {'RUNNING_MODAL'}

    def cleanup(self, context):
        if self._handle: bpy.types.SpaceView3D.draw_handler_remove(self._handle, 'WINDOW')
        if self._timer: context.window_manager.event_timer_remove(self._timer)
        for h in bpy.app.handlers.depsgraph_update_post:
            if h.__name__ == "easysection_depsgraph_callback": bpy.app.handlers.depsgraph_update_post.remove(h)
        context.scene.easysection_is_running = False

def update_realtime_mode(self, context):
    target_obj = bpy.data.objects.get(TARGET_NAME); box_obj = bpy.data.objects.get(PROXY_NAME)
    if target_obj and GN_MOD_NAME in target_obj.modifiers:
        try:
            is_active = bool(self.dha_sync_active)
            target_obj.modifiers[GN_MOD_NAME][REALTIME_SOCKET] = is_active
            if is_active:
                target_obj.modifiers[GN_MOD_NAME][BOX_SOCKET] = box_obj
                for cam_name, arrow_name in CAM_ARROW_PAIRS.items():
                    cam = bpy.data.objects.get(cam_name); arrow = bpy.data.objects.get(arrow_name)
                    if cam and arrow:
                        const = cam.constraints.new(type='COPY_TRANSFORMS')
                        const.name = "EasySection_Sync"; const.target = arrow
            else:
                sync_evaluated_mesh(); target_obj.modifiers[GN_MOD_NAME][BOX_SOCKET] = None
                for cam_name, _ in CAM_ARROW_PAIRS.items():
                    cam = bpy.data.objects.get(cam_name)
                    if cam and "EasySection_Sync" in cam.constraints: cam.constraints.remove(cam.constraints["EasySection_Sync"])
            force_viewport_update(target_obj, context)
        except: pass

def update_wire(self, context):
    obj = bpy.data.objects.get(WIRE_OBJ_NAME)
    if obj and WIRE_MOD_NAME in obj.modifiers:
        try:
            obj.modifiers[WIRE_MOD_NAME]["Socket_3"] = int(self.dha_wire_mode)
            obj.modifiers[WIRE_MOD_NAME]["Socket_4"] = self.dha_wire_color
            obj.modifiers[WIRE_MOD_NAME]["Socket_5"] = self.dha_wire_slider
            force_viewport_update(obj, context)
        except: pass

@persistent
def undo_pre_handler(scene): global is_undoing; is_undoing = True
@persistent
def undo_post_handler(scene):
    global is_undoing, last_transforms; is_undoing = False
    source_obj = bpy.data.objects.get(PROXY_NAME)
    if source_obj: last_transforms = get_tracked_transforms(source_obj)

def update_capholes(self, context):
    if not self.section_collection: return
    for obj in self.section_collection.all_objects:
        if obj.type == 'MESH': logic.set_geo_input(obj, logic.GEO_SYNC, "Enable Capholes", self.enable_capholes)

def update_invert(self, context):
    if self.section_collection:
        for obj in self.section_collection.all_objects:
            if obj.type == 'MESH': logic.set_geo_input(obj, logic.GEO_SYNC, "Invert", self.invert_section)
    if self.object_collection:
        for obj in self.object_collection.all_objects:
            if obj.type == 'MESH' or obj.name.endswith("_controller"):
                logic.set_geo_input(obj, logic.GEO_SINGLE, "Invert Hide", self.invert_section)
                logic.set_geo_input(obj, logic.GEO_PARENT, "Invert Hide", self.invert_section)
    
    arrow_obj = bpy.data.objects.get("DHAArrow")
    if arrow_obj: logic.set_geo_input(arrow_obj, "GeometryNodes", "Invert Arrow", self.invert_section)

    slicer_obj = bpy.data.objects.get(logic.SLICER_NAME)
    if slicer_obj:
        mod = slicer_obj.modifiers.get("GeometryNodes")
        if mod and "Socket_2" in mod:
            mod["Socket_2"] = self.invert_section
            mod.show_viewport = mod.show_viewport

def update_cap_innerholes(self, context):
    if not self.section_collection: return
    for obj in self.section_collection.all_objects:
        if obj.type == 'MESH': logic.set_geo_input(obj, logic.GEO_SYNC, "FixNormal", self.cap_innerholes)

def update_cap_material(self, context):
    if not self.section_collection: return
    for obj in self.section_collection.all_objects:
        if obj.type == 'MESH': logic.set_geo_input(obj, logic.GEO_SYNC, "CapMaterial", self.cap_material)

def update_enable_cap_mat(self, context):
    if not self.section_collection: return
    for obj in self.section_collection.all_objects:
        if obj.type == 'MESH': logic.set_geo_input(obj, logic.GEO_SYNC, "Enable CapMaterial", self.enable_cap_mat)

def update_arrow_type(self, context):
    arrow_obj = bpy.data.objects.get("DHAArrow")
    if arrow_obj and "GeometryNodes" in arrow_obj.modifiers:
        arrow_obj.modifiers["GeometryNodes"]["Socket_2"] = self.arrow_type
        arrow_obj.modifiers["GeometryNodes"].show_viewport = arrow_obj.modifiers["GeometryNodes"].show_viewport

def update_arrow_scale(self, context):
    arrow_obj = bpy.data.objects.get("DHAArrow")
    if arrow_obj and "GeometryNodes" in arrow_obj.modifiers:
        arrow_obj.modifiers["GeometryNodes"]["Socket_3"] = self.arrow_scale
        arrow_obj.modifiers["GeometryNodes"].show_viewport = arrow_obj.modifiers["GeometryNodes"].show_viewport

def refresh_all_ui_updates(props, context):
    update_capholes(props, context)
    update_invert(props, context)
    update_cap_innerholes(props, context)
    update_enable_cap_mat(props, context)
    update_cap_material(props, context)
    update_arrow_type(props, context)
    update_arrow_scale(props, context)

class EasySectionProperties(bpy.types.PropertyGroup):
    section_collection: bpy.props.PointerProperty(type=bpy.types.Collection)
    object_collection: bpy.props.PointerProperty(type=bpy.types.Collection)
    
    offset: bpy.props.FloatProperty(default=0.5, min=0.0)

    enable_capholes: bpy.props.BoolProperty(name="Enable CapHoles", update=update_capholes)
    invert_section: bpy.props.BoolProperty(name="Invert", update=update_invert)
    cap_innerholes: bpy.props.BoolProperty(name="Cap InnerHoles", update=update_cap_innerholes)
    enable_cap_mat: bpy.props.BoolProperty(name="Enable CapMaterial", update=update_enable_cap_mat)
    cap_material: bpy.props.PointerProperty(name="CapMaterial", type=bpy.types.Material, update=update_cap_material)
    arrow_type: bpy.props.IntProperty(name="Arrow Type", default=0, min=0, update=update_arrow_type)
    arrow_scale: bpy.props.FloatProperty(name="Arrow Scale", default=1.0, min=0.0, update=update_arrow_scale)
    
    outline_enable: bpy.props.BoolProperty(name="Outline", update=logic.update_global_outline)
    outline_thickness: bpy.props.FloatProperty(name="Thickness", default=1.0, min=0.0, update=logic.update_global_outline_thickness)
    outline_color: bpy.props.FloatVectorProperty(name="Color", subtype='COLOR', default=(0,0,0,1), size=4, min=0.0, max=1.0, update=logic.update_global_outline_color)

class EasySectionState(bpy.types.PropertyGroup):
    state_name: bpy.props.StringProperty(default="New State")
    state_data: bpy.props.StringProperty()

class EASYSECTION_OT_ApplySection(bpy.types.Operator):
    bl_idname = "easysection.apply_section"
    bl_label = "Apply Section & Objects Sync"
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context):
        props = context.scene.easy_section_props
        if not props.section_collection:
            self.report({'ERROR'}, get_msg("Please select Section Collection", "يرجى تحديد مجموعة القطاعات"))
            return {'CANCELLED'}

        cols_to_import = [c for c in [logic.COL_NAME] + logic.HIDDEN_COLS if c not in bpy.data.collections]
        nodes_to_import = [n for n in [logic.SHADER_NAME, logic.GEO_SYNC, logic.GEO_SINGLE, logic.GEO_PARENT, "DHALineArt"] if n not in bpy.data.node_groups]
        texts_to_import = [logic.SCRIPT_NAME] if logic.SCRIPT_NAME not in bpy.data.texts else []
        mats_to_import = ["MABlack"] if "MABlack" not in bpy.data.materials else []

        if cols_to_import or nodes_to_import or texts_to_import or mats_to_import:
            file_path = logic.ASSETS_PATH
            if not os.path.exists(file_path):
                self.report({'ERROR'}, get_msg("Asset file not found, check path", "ملف المصدر غير موجود، تأكد من المسار"))
                return {'CANCELLED'}

            with bpy.data.libraries.load(file_path, link=False) as (data_from, data_to):
                for c in cols_to_import:
                    if c in data_from.collections: data_to.collections.append(c)
                for n in nodes_to_import:
                    if n in data_from.node_groups: data_to.node_groups.append(n)
                for t in texts_to_import:
                    if t in data_from.texts: data_to.texts.append(t)
                for m in mats_to_import:
                    if m in data_from.materials: data_to.materials.append(m)

        main_col = bpy.data.collections.get(logic.COL_NAME)
        if main_col and main_col.name not in context.scene.collection.children:
            context.scene.collection.children.link(main_col)

        for hc in logic.HIDDEN_COLS:
            col_to_hide = bpy.data.collections.get(hc)
            if col_to_hide:
                col_to_hide.hide_viewport = True
                col_to_hide.hide_render = True
                if col_to_hide.name in context.scene.collection.children:
                    context.scene.collection.children.unlink(col_to_hide)

        if logic.process_sync_logic(self, context, props, is_update=False):
            if logic.SCRIPT_NAME in bpy.data.texts:
                try: 
                    custom_globals = {"bpy": bpy, "mathutils": mathutils}
                    exec(bpy.data.texts[logic.SCRIPT_NAME].as_string(), custom_globals)
                except Exception as e: 
                    self.report({'WARNING'}, get_msg(f"Script Error: {str(e)}", f"خطأ بالسكريبت: {str(e)}"))
            
            refresh_all_ui_updates(props, context)
            
            global gizmo_storage, last_transforms, idle_ticks
            gizmo_storage = GizmoData()
            last_transforms = None 
            idle_ticks = 0         

            context.view_layer.update() 
            sync_evaluated_mesh()        
            
            self.report({'INFO'}, get_msg("Sync Successful!", "تمت المزامنة بنجاح!"))

        if not context.scene.easysection_is_running:
            bpy.ops.easysection.interactive_arrow('INVOKE_DEFAULT')

        return {'FINISHED'}

class EASYSECTION_OT_UpdateSection(bpy.types.Operator):
    bl_idname = "easysection.update_section"
    bl_label = "Update Objects"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        props = context.scene.easy_section_props
        
        if logic.process_sync_logic(self, context, props, is_update=True):
            if logic.SCRIPT_NAME in bpy.data.texts:
                try: 
                    custom_globals = {"bpy": bpy, "mathutils": mathutils}
                    exec(bpy.data.texts[logic.SCRIPT_NAME].as_string(), custom_globals)
                except: pass
            refresh_all_ui_updates(props, context)
            
            context.view_layer.update()
            self.report({'INFO'}, get_msg("Objects updated successfully!", "تم تحديث العناصر بنجاح!"))

        if context.scene.easysection_is_running:
            context.scene.easysection_is_running = False
            
            def auto_restart_gizmo():
                global gizmo_storage, last_transforms, idle_ticks
                
                gizmo_storage = GizmoData()
                last_transforms = None
                idle_ticks = 0
                
                override_context = None
                for window in bpy.context.window_manager.windows:
                    for area in window.screen.areas:
                        if area.type == 'VIEW_3D':
                            for region in area.regions:
                                if region.type == 'WINDOW':
                                    override_context = {
                                        'window': window,
                                        'screen': window.screen,
                                        'area': area,
                                        'region': region
                                    }
                                    break
                            if override_context: break
                    if override_context: break

                if override_context:
                    try:
                        with bpy.context.temp_override(**override_context):
                            bpy.ops.easysection.interactive_arrow('INVOKE_DEFAULT')
                    except Exception as e:
                        print("Gizmo Restart Error:", e)
                else:
                    bpy.ops.easysection.interactive_arrow('INVOKE_DEFAULT')
                    
                return None 
                
            bpy.app.timers.register(auto_restart_gizmo, first_interval=0.1)
        else:
            bpy.ops.easysection.interactive_arrow('INVOKE_DEFAULT')

        return {'FINISHED'}

class EASYSECTION_OT_RemoveSection(bpy.types.Operator):
    bl_idname = "easysection.remove_section"
    bl_label = "Remove Setup"
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context):
        props = context.scene.easy_section_props
        logic.process_remove_logic(props, context)
        context.scene.easysection_is_running = False
        self.report({'INFO'}, get_msg("Setup removed from scene!", "تم إزالة القطاع من المشهد!"))
        return {'FINISHED'}

class EASYSECTION_OT_ApplySelectedCap(bpy.types.Operator):
    bl_idname = "easysection.apply_selected_cap"
    bl_label = "Apply to Selected"
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context):
        scene = context.scene
        count = 0
        for obj in context.selected_objects:
            if obj.type == 'MESH':
                gn = obj.modifiers.get(logic.GEO_SYNC)
                if gn:
                    if scene.es_sel_apply_mat: logic.set_geo_input(obj, logic.GEO_SYNC, "CapMaterial", scene.es_sel_cap_mat)
                    if scene.es_sel_apply_hatch:
                        if "Socket_17" in gn: gn["Socket_17"] = int(scene.es_sel_hatch_type)
                    if scene.es_sel_apply_out_en:
                        if "Socket_16" in gn: gn["Socket_16"] = scene.es_sel_outline_enable
                    if scene.es_sel_apply_out_th:
                        if "Socket_14" in gn: gn["Socket_14"] = scene.es_sel_outline_thickness
                    if scene.es_sel_apply_out_col:
                        if "Socket_15" in gn: gn["Socket_15"] = scene.es_sel_outline_color
                    gn.show_viewport = gn.show_viewport
                count += 1
        self.report({'INFO'}, get_msg(f"Applied settings to {count} objects.", f"تم تطبيق الإعدادات المحددة على {count} عنصر."))
        return {'FINISHED'}

class EASYSECTION_OT_StateSave(bpy.types.Operator):
    bl_idname = "easysection.state_save"
    bl_label = "Save State"
    
    def execute(self, context):
        state_data = logic.get_slicer_state()
        if not state_data:
            self.report({'WARNING'}, get_msg("SlicerBox not found!", "SlicerBox غير موجود!"))
            return {'CANCELLED'}
            
        scene = context.scene
        base_name = scene.es_new_state_name 
        
        existing_names = {s.state_name for s in scene.es_slicer_states}
        unique_name = base_name
        counter = 1
        
        while unique_name in existing_names:
            unique_name = f"{base_name}.{counter:03d}"
            counter += 1
        
        new_state = scene.es_slicer_states.add()
        new_state.state_name = unique_name
        new_state.state_data = state_data
        
        scene.es_slicer_states_index = len(scene.es_slicer_states) - 1
        self.report({'INFO'}, get_msg(f"Saved successfully as: {unique_name}", f"تم الحفظ بنجاح باسم: {unique_name}"))
        return {'FINISHED'}

class EASYSECTION_OT_StateRestore(bpy.types.Operator):
    bl_idname = "easysection.state_restore"
    bl_label = "Restore State"
    index: bpy.props.IntProperty()
    def execute(self, context):
        states = context.scene.es_slicer_states
        if 0 <= self.index < len(states):
            logic.restore_slicer_state(states[self.index].state_data)
        return {'FINISHED'}

class EASYSECTION_OT_StateRemove(bpy.types.Operator):
    bl_idname = "easysection.state_remove"
    bl_label = "Remove State"
    index: bpy.props.IntProperty()
    def execute(self, context):
        context.scene.es_slicer_states.remove(self.index)
        context.scene.es_slicer_states_index = max(0, self.index - 1)
        return {'FINISHED'}

class EASYSECTION_OT_SelectPreviewGroup(bpy.types.Operator):
    bl_idname = "easysection.select_preview_group"
    bl_label = "Select"
    grp_name: bpy.props.StringProperty()
    def execute(self, context):
        grp_col = bpy.data.collections.get(self.grp_name)
        if grp_col:
            bpy.ops.object.select_all(action='DESELECT')
            objs = list(grp_col.objects)
            visible_objs = [o for o in objs if not o.hide_viewport]
            for obj in visible_objs: obj.select_set(True)
            if visible_objs: context.view_layer.objects.active = visible_objs[0]
        return {'FINISHED'}

class EASYSECTION_OT_ToggleHideGroup(bpy.types.Operator):
    bl_idname = "easysection.toggle_hide_group"
    bl_label = "Hide"
    grp_name: bpy.props.StringProperty()
    def execute(self, context):
        grp_col = bpy.data.collections.get(self.grp_name)
        if grp_col: grp_col.hide_viewport = not grp_col.hide_viewport
        return {'FINISHED'}

class EASYSECTION_OT_DeletePreviewGroup(bpy.types.Operator):
    bl_idname = "easysection.delete_preview_group"
    bl_label = "Delete"
    bl_options = {'REGISTER', 'UNDO'}
    grp_name: bpy.props.StringProperty()
    def execute(self, context):
        logic.delete_preview_group(self.grp_name)
        return {'FINISHED'}

class EASYSECTION_OT_RenameGroup(bpy.types.Operator):
    bl_idname = "easysection.rename_preview_group"
    bl_label = "Rename Elevation"
    bl_options = {'REGISTER', 'UNDO'}
    
    grp_name: bpy.props.StringProperty()
    new_name: bpy.props.StringProperty(name="New Name")
    
    def invoke(self, context, event):
        self.new_name = self.grp_name
        return context.window_manager.invoke_props_dialog(self)
        
    def execute(self, context):
        if not self.new_name or self.new_name == self.grp_name:
            return {'CANCELLED'}
        grp_col = bpy.data.collections.get(self.grp_name)
        if grp_col:
            grp_col.name = self.new_name
            final_name = grp_col.name 
            for obj in grp_col.objects:
                if "es_elevation_group" in obj:
                    obj["es_elevation_group"] = final_name
        return {'FINISHED'}

class EASYSECTION_OT_SetupGroup(bpy.types.Operator):
    bl_idname = "easysection.setup_lineart_group"
    bl_label = "Create Elevation"
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context):
        try: 
            logic.create_live_preview_group(context.scene.es_new_preview_name, context.scene.easy_section_props)
        except Exception as e: 
            err_msg = str(e)
            
            if err_msg == "MISSING_ARROW":
                en_text = "Please select an arrow first to create elevation!"
                ar_text = "يرجى تحديد السهم المطلوب أولاً لإنشاء الواجهة من اتجاهه!"
                err_msg = get_msg(en_text, ar_text)
                
            self.report({'ERROR'}, err_msg)
            
        return {'FINISHED'}

class EASYSECTION_OT_FreezeGroup(bpy.types.Operator):
    bl_idname = "easysection.freeze_group"
    bl_label = "Freeze"
    bl_options = {'REGISTER', 'UNDO'}
    grp_name: bpy.props.StringProperty()
    def execute(self, context):
        logic.freeze_group(self.grp_name)
        return {'FINISHED'}

class EASYSECTION_OT_UnfreezeGroup(bpy.types.Operator):
    bl_idname = "easysection.unfreeze_group"
    bl_label = "Unfreeze"
    bl_options = {'REGISTER', 'UNDO'}
    grp_name: bpy.props.StringProperty()
    def execute(self, context):
        logic.unfreeze_group(self.grp_name)
        return {'FINISHED'}

class EASYSECTION_OT_ExportGroup(bpy.types.Operator, ExportHelper):
    bl_idname = "easysection.export_dxf_group"
    bl_label = "Export DXF"
    
    filename_ext = ".dxf"
    filter_glob: bpy.props.StringProperty(default="*.dxf", options={'HIDDEN'})
    grp_name: bpy.props.StringProperty()
    
    filepath: bpy.props.StringProperty(
        name="File Path",
        description="Filepath used for exporting the file",
        maxlen=1024,
        default=""
    )
    
    def invoke(self, context, event):
        dir_path = os.path.dirname(self.filepath) if self.filepath else ""
        filename = f"{self.grp_name}_{datetime.now().strftime('%H%M%S')}.dxf"
        self.filepath = os.path.join(dir_path, filename)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}
        
    def execute(self, context):
        try: 
            logic.export_preview_group(self.grp_name, self.filepath)
            self.report({'INFO'}, get_msg(f"Exported successfully: {self.filepath}", f"تم التصدير بنجاح: {self.filepath}"))
        except Exception as e: 
            self.report({'ERROR'}, str(e))
        return {'FINISHED'}

class EASYSECTION_PT_SectionPanel(bpy.types.Panel):
    bl_label = "Section Builder"
    bl_idname = "EASYSECTION_PT_SectionPanel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'EasySection'

    @classmethod
    def poll(cls, context):
        # هنخلي البانل تظهر دايماً عشان زرار الـ Remove يفضل متاح
        return True

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        # الوصول للإعدادات للتأكد من حالة التفعيل
        prefs = context.preferences.addons.get(__package__).preferences
        
        # 1. حالة الإضافة "غير مفعلة" (وضع الطوارئ)
        if not prefs.is_verified:
            box = layout.box()
            col = box.column(align=True)
            
            col.alert = True
            col.label(text=get_msg("Addon Locked", "الإضافة مغلقة"), icon='LOCKED')
            col.separator()
            
            # زرار الإزالة متاح دايماً لليوزر حتى لو الرخصة طفت
            col.operator("easysection.remove_section", text=get_msg("Remove Section", "إزالة القطاع"), icon='TRASH')
            
            col.separator()
            # زرار سريع لفتح التفعيل بدل ما يروح للبيرفرنسيز
            col.operator("easysection.license_popup", text=get_msg("Activate Pro", "تفعيل النسخة البرو"), icon='CHECKMARK')
            
            return # إنهاء الدالة هنا عشان ميرسمش باقي الأدوات

        # 2. حالة الإضافة "مفعلة" (المحتوى الأصلي بتاعك)
        props = scene.easy_section_props
        
        col = layout.column(align=True)
        col.label(text=get_msg("Section Collection:", "مجموعة القطاعات:"))
        col.prop(props, "section_collection", text="")
        
        col.separator(factor=0.3)
        col.label(text=get_msg("Object Collection (Furniture):", "مجموعة العناصر (الأثاث):"))
        col.prop(props, "object_collection", text="")
        
        col.separator()
        row_offset = col.row()
        row_offset.prop(props, "offset", text=get_msg("Boundary Offset (m)", "مسافة الإزاحة (م)"))
        
        layout.separator(factor=0.5)

        is_applied = bpy.data.objects.get(PROXY_NAME) is not None

        row_apply = layout.row()
        row_apply.enabled = not is_applied 
        row_apply.operator("easysection.apply_section", text=get_msg("Start Section!", "تطبيق جميع العمليات"), icon='GEOMETRY_NODES')
        
        row_ops = layout.row(align=True)
        row_ops.enabled = is_applied 
        row_ops.operator("easysection.update_section", text=get_msg("Update", "تحديث"), icon='FILE_REFRESH')
        row_ops.operator("easysection.remove_section", text=get_msg("Remove", "إزالة"), icon='TRASH')

class EASYSECTION_PT_SectionModifiers(bpy.types.Panel):
    bl_label = "Section Modifiers"
    bl_idname = "EASYSECTION_PT_SectionModifiers"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'EasySection'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        # تأكد إن الـ ID هنا هو نفس اسم فولدر الإضافة عندك
        prefs = context.preferences.addons.get(__package__)
        if prefs:
            return prefs.preferences.is_verified
        return False

    def draw(self, context):
        layout = self.layout
        props = context.scene.easy_section_props
        scene = context.scene
        
        slicer_box = layout.box()
        s_col = slicer_box.column(align=True)
        
        header_row = s_col.row(align=True)
        fold_icon = 'TRIA_DOWN' if scene.es_show_slicer_views else 'TRIA_RIGHT'
        
        header_row.prop(scene, "es_show_slicer_views", text=get_msg("Slicer Saved Views", "اللقطات المحفوظة للسلايسر"), icon='MOD_WIREFRAME', toggle=True, emboss=False)
        header_row.prop(scene, "es_show_slicer_views", text="", icon=fold_icon, toggle=True, emboss=False)

        if scene.es_show_slicer_views:
            s_col.separator(factor=0.7)
            row_state = s_col.row(align=True)
            row_state.prop(scene, "es_new_state_name", text="")
            row_state.operator("easysection.state_save", text="", icon='ADD')
            if scene.es_slicer_states:
                s_col.separator(factor=0.4)
                s_col.template_list("EASYSECTION_UL_SlicerStates", "", scene, "es_slicer_states", scene, "es_slicer_states_index", rows=2)

        layout.separator(factor=0.1)
        
        box2 = layout.box()
        col2 = box2.column(align=True)
        h2 = col2.row(align=True)
        icon2 = 'TRIA_DOWN' if scene.es_show_live_mods else 'TRIA_RIGHT'
        h2.prop(scene, "es_show_live_mods", text=get_msg("Live Modifier", "المعدلات المباشرة"), icon='MODIFIER', toggle=True, emboss=False)
        h2.prop(scene, "es_show_live_mods", text="", icon=icon2, toggle=True, emboss=False)

        if scene.es_show_live_mods:
            col2.separator(factor=0.6)
            col2.prop(props, "enable_capholes", text=get_msg("Enable Capholes", "تفعيل سد الفتحات"), toggle=True)
            col2.separator(factor=0.4)
            row_clean = col2.row(align=True)
            row_clean.prop(props, "cap_innerholes", text=get_msg("Cleaner", "تنظيف"), toggle=True)
            row_clean.prop(props, "invert_section", text=get_msg("Invert", "قلب القطاع"), toggle=True)
            
            row_mat = col2.row(align=True)
            row_mat.prop(props, "enable_cap_mat", text=get_msg("Cap Material", "خامة القطع"), toggle=True)
            row_mat.prop(props, "cap_material", text="")
            col2.separator(factor=0.8)
            
            col2.prop(props, "outline_enable", text=get_msg("Outlines", "تفعيل خطوط التحديد"), toggle=True, icon='SHADING_RENDERED')
            if props.outline_enable:
                col2.separator(factor=0.4)
                row_out = col2.row(align=True)
                row_out.prop(props, "outline_thickness", text=get_msg("Thick", "السمك"))
                row_out.prop(props, "outline_color", text="")

        layout.separator(factor=0.1)
        
        box3 = layout.box()
        col3 = box3.column(align=True)
        h3 = col3.row(align=True)
        icon3 = 'TRIA_DOWN' if scene.es_show_selected_setup else 'TRIA_RIGHT'
        h3.prop(scene, "es_show_selected_setup", text=get_msg("Selected Modifier", "معدل العنصر المحدد"), icon='RESTRICT_SELECT_OFF', toggle=True, emboss=False)
        h3.prop(scene, "es_show_selected_setup", text="", icon=icon3, toggle=True, emboss=False)

        if scene.es_show_selected_setup:
            col3.separator(factor=0.6)
            r_mat = col3.row(align=True)
            r_mat.prop(scene, "es_sel_apply_mat", text="", icon='MATERIAL')
            r_mat.prop(scene, "es_sel_cap_mat", text=get_msg("Material", "الخامة"))
            
            col3.separator(factor=0.3)
            r_hat = col3.row(align=True)
            r_hat.prop(scene, "es_sel_apply_hatch", text="", icon='TEXTURE')
            r_hat.prop(scene, "es_sel_hatch_type", text=get_msg("Hatch", "التهشير"))
            
            col3.separator(factor=0.3)
            r_out = col3.row(align=True)
            r_out.prop(scene, "es_sel_apply_out_en", text="", icon='SHADING_RENDERED')
            r_out.label(text=get_msg("Outline", "الخط الخارجي"))
            r_out.prop(scene, "es_sel_outline_enable", text=get_msg("ON", "تشغيل") if scene.es_sel_outline_enable else get_msg("OFF", "إيقاف"), toggle=True)

            col3.separator(factor=0.3)
            r_thick = col3.row(align=True)
            r_thick.prop(scene, "es_sel_apply_out_th", text="", icon='MOD_THICKNESS')
            r_thick.label(text=get_msg("Thickness", "السمك"))
            r_thick.prop(scene, "es_sel_outline_thickness", text="")
            
            col3.separator(factor=0.3)
            r_col = col3.row(align=True)
            r_col.prop(scene, "es_sel_apply_out_col", text="", icon='COLOR')
            r_col.label(text=get_msg("Color", "اللون"))
            r_col.prop(scene, "es_sel_outline_color", text="")
            
            col3.separator(factor=0.8)
            col3.operator("easysection.apply_selected_cap", text=get_msg("Apply to Selected", "تطبيق على المحدد"), icon='CHECKMARK')

class EASYSECTION_PT_ElevationPanel(bpy.types.Panel):
    bl_label = "Sections & Elevations Exporter"
    bl_idname = "EASYSECTION_PT_ElevationPanel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'EasySection'

    @classmethod
    def poll(cls, context):
        # تأكد إن الـ ID هنا هو نفس اسم فولدر الإضافة عندك
        prefs = context.preferences.addons.get(__package__)
        if prefs:
            return prefs.preferences.is_verified
        return False

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        main_box = layout.box()
        main_col = main_box.column(align=True)
        row_new = main_col.row(align=True)
        row_new.prop(scene, "es_new_preview_name", text="")
        row_new.operator("easysection.setup_lineart_group", text=get_msg("New", "جديد"), icon='ADD')

        col_previews = bpy.data.collections.get("ES_Previews")
        if col_previews and col_previews.children:
            main_col.separator(factor=0.5)
            main_col.template_list("EASYSECTION_UL_Elevations", "", col_previews, "children", scene, "es_elevation_index", rows=2)

            if 0 <= scene.es_elevation_index < len(col_previews.children):
                grp_col = col_previews.children[scene.es_elevation_index]
                active_grp = grp_col.name
                is_frozen = grp_col.get("es_is_frozen", 0)
                
                layout.separator(factor=0.3) 
                set_box = layout.box()
                
                active_objs = list(grp_col.objects)
                visible_objs = [o for o in active_objs if not o.hide_viewport]
                obj_ele = next((o for o in visible_objs if o.get("es_preview_type") == "_Ele"), None)
                obj_obj = next((o for o in visible_objs if o.get("es_preview_type") == "_Obj"), None)
                obj_cut = next((o for o in visible_objs if o.get("es_preview_type") == "_Cut"), None)

                if obj_ele or obj_obj:
                    l_box = set_box.box()
                    l_col = l_box.column(align=True)
                    
                    def draw_line_settings(obj, title):
                        l_col.label(text=title, icon='GREASEPENCIL')
                        gn = obj.modifiers.get("DHALineArt")
                        
                        r_crease = l_col.row(align=True)
                        r_crease.prop(obj, "es_use_crease", text="", toggle=True, icon='MOD_EDGESPLIT')
                        
                        sub_r = r_crease.row(align=True)
                        sub_r.active = obj.es_use_crease
                        sub_r.prop(obj, "es_crease_angle", text=get_msg("Angle Crease", "زاوية الكسرة"))
                        
                        if gn:
                            r_params = l_col.row(align=True)
                            r_params.prop(gn, '["Socket_5"]', text=get_msg("Thick", "السمك"))
                            r_params.prop(gn, '["Socket_4"]', text="")
                            if not grp_col.es_link_offsets:
                                l_col.prop(gn, '["Socket_2"]', text=get_msg("Local Offset", "إزاحة محلية"))
                            if not grp_col.es_link_depth:
                                l_col.prop(obj, "es_fade", text=get_msg("Fade", "تلاشي"))
                                r_local_mm = l_col.row(align=True)
                                r_local_mm.prop(obj, "es_min_dist", text=get_msg("Min", "أدنى"))
                                r_local_mm.prop(obj, "es_max_dist", text=get_msg("Max", "أقصى"))

                    if obj_ele: draw_line_settings(obj_ele, get_msg("Elevation Lines", "خطوط الواجهة"))
                    if obj_ele and obj_obj: l_col.separator(factor=1.2)
                    if obj_obj: draw_line_settings(obj_obj, get_msg("Furniture Lines", "خطوط الأثاث"))

                if obj_cut:
                    c_box = set_box.box()
                    c_col = c_box.column(align=True)
                    c_col.label(text=get_msg("Section Lines", "خطوط القطع"), icon='MOD_LINEART')
                    gn_cut = obj_cut.modifiers.get("DHALineArt")
                    if gn_cut:
                        r_c = c_col.row(align=True)
                        r_c.prop(gn_cut, '["Socket_5"]', text=get_msg("Thick", "السمك"))
                        r_c.prop(gn_cut, '["Socket_4"]', text="")
                        if not grp_col.es_link_offsets:
                            c_col.prop(gn_cut, '["Socket_2"]', text=get_msg("Local Offset", "إزاحة محلية"))
                        c_col.separator(factor=0.5)
                        c_col.prop(obj_cut, "es_fill_type")
                        c_col.prop(obj_cut, "es_hatch_scale")

                set_box.separator(factor=0.0) 
                g_col = set_box.column(align=True)
                
                row_off = g_col.row(align=True)
                row_off.prop(grp_col, "es_link_offsets", text="", icon='LINKED' if grp_col.es_link_offsets else 'UNLINKED')
                row_off.label(text=get_msg("Global Offset", "إزاحة عامة"))
                if grp_col.es_link_offsets: 
                    row_off.prop(grp_col, "es_group_offset", text="")
                
                g_col.separator(factor=0.3)
                
                row_d1 = g_col.row(align=True)
                f_ico = 'NODE_INSERT_ON' if grp_col.es_link_depth else 'NODE_INSERT_OFF'
                row_d1.prop(grp_col, "es_link_depth", text="", icon=f_ico)
                
                if grp_col.es_link_depth:
                    row_d1.prop(grp_col, "es_global_fade", text=get_msg("Global Depth", "عمق عام"))
                    row_d2 = g_col.row(align=True)
                    row_d2.prop(grp_col, "es_global_min", text=get_msg("Min", "أدنى"))
                    row_d2.prop(grp_col, "es_global_max", text=get_msg("Max", "أقصى"))
                else:
                    row_d1.label(text=get_msg("Global Depth", "عمق عام"))
                
                set_box.separator(factor=0.0) 
                row_act = set_box.row(align=True)
                
                row_act.enabled = not grp_col.hide_viewport
                
                if is_frozen:
                    row_act.operator("easysection.unfreeze_group", text=get_msg("Unfreeze", "إلغاء التجميد"), icon='FREEZE').grp_name = active_grp
                else:
                    row_act.operator("easysection.freeze_group", text=get_msg("Freeze", "تجميد"), icon='FREEZE').grp_name = active_grp
                
                row_act.operator("easysection.export_dxf_group", text=get_msg("Export DXF", "تصدير DXF"), icon='EXPORT').grp_name = active_grp

class EASYSECTION_UL_SlicerStates(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            layout.prop(item, "state_name", text="", emboss=False, icon='RESTRICT_VIEW_OFF')
            op_del = layout.operator("easysection.state_remove", text="", icon='TRASH')
            op_del.index = index

class EASYSECTION_UL_Elevations(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        col = item
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            is_frozen = col.get("es_is_frozen", 0)
            draw_icon = 'FREEZE' if is_frozen else 'NODE_SOCKET_SHADER'
            
            layout.prop(col, "es_elevation_name", text="", emboss=False, icon=draw_icon)
            
            op_h = layout.operator("easysection.toggle_hide_group", text="", icon='HIDE_ON' if col.hide_viewport else 'HIDE_OFF')
            op_h.grp_name = col.name
            op_d = layout.operator("easysection.delete_preview_group", text="", icon='TRASH')
            op_d.grp_name = col.name

class SECTION_PT_customization(bpy.types.Panel):
    bl_label = "Section Customization"
    bl_idname = "SECTION_PT_customization"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'EasySection'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        # تأكد إن الـ ID هنا هو نفس اسم فولدر الإضافة عندك
        prefs = context.preferences.addons.get(__package__)
        if prefs:
            return prefs.preferences.is_verified
        return False

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        layout.label(text=get_msg("Slicer Controls", "إعدادات السلايسر"), icon='MOD_BOOLEAN')
        box_s = layout.box()
        
        box_s.label(text=get_msg("Performance", "الأداء"), icon='PROPERTIES')
        col_perf = box_s.column(align=True)
        col_perf.prop(scene, "dha_sync_active", text=get_msg("Realtime Mode", "الوضع المباشر"), toggle=True)
        
        box_s.label(text=get_msg("Wire Settings", "إعدادات الخطوط"), icon='MOD_WIREFRAME')
        col_wire = box_s.column(align=True)
        row_style = col_wire.row(align=True)
        row_style.label(text=get_msg("Style", "الشكل"))
        row_style.prop(scene, "dha_wire_mode", text="")
        
        if scene.dha_wire_mode != '2':
            row_vc = col_wire.row(align=True)
            row_vc.prop(scene, "dha_wire_slider", text=get_msg("Thickness", "السمك"))
            row_vc.prop(scene, "dha_wire_color", text="")

        layout.separator(factor=0.5)

        layout.label(text=get_msg("Gizmo Controls", "إعدادات الجيزمو"), icon='EMPTY_ARROWS')
        box_g = layout.box()
        
        box_g.label(text=get_msg("Performance", "الأداء"), icon='PROPERTIES')
        col_g_perf = box_g.column(align=True)
        
        if not scene.easysection_is_running:
            col_g_perf.operator("easysection.interactive_arrow", text=get_msg("Turn on Gizmos", "تشغيل الجيزمو"), icon='PLAY')
        else:
            col_g_perf.prop(scene, "easysection_is_running", text=get_msg("Turn off Gizmos", "إيقاف الجيزمو"), toggle=True, icon='CANCEL')
            
        col_g_perf.prop(scene, "easysection_update_interval", text=get_msg("Refresh", "تحديث"))
        col_g_perf.prop(scene, "easysection_drag_sensitivity", text=get_msg("Speed", "السرعة"))
        col_g_perf.prop(scene, "easysection_use_occlusion", text=get_msg("Occlusion (Raycast)", "حجب الرؤية"))
        col_g_perf.prop(scene, "easysection_use_undo", text=get_msg("Record Undo", "تسجيل التراجع"))
        
        box_g.label(text=get_msg("Gizmo Settings", "إعدادات الجيزمو"), icon='RESTRICT_SELECT_OFF')
        row_gs = box_g.row(align=True)
        row_gs.prop(scene, "easysection_arrow_size", text=get_msg("Size", "الحجم"))
        row_gs.prop(scene, "easysection_arrow_color", text="")

# ==========================================
# 4. تجميع الكلاسات و تسجيل الخصائص
# ==========================================

ui_classes = [
    EasySectionState,
    EasySectionProperties,
    EASYSECTION_OT_ApplySection,
    EASYSECTION_OT_UpdateSection,
    EASYSECTION_OT_RemoveSection,
    EASYSECTION_OT_ApplySelectedCap,
    EASYSECTION_OT_StateSave,
    EASYSECTION_OT_StateRestore,
    EASYSECTION_OT_StateRemove,
    EASYSECTION_OT_SelectPreviewGroup,
    EASYSECTION_OT_ToggleHideGroup,
    EASYSECTION_OT_DeletePreviewGroup,
    EASYSECTION_OT_RenameGroup,
    EASYSECTION_OT_SetupGroup,
    EASYSECTION_OT_FreezeGroup,
    EASYSECTION_OT_UnfreezeGroup,
    EASYSECTION_OT_ExportGroup,
    EASYSECTION_PT_SectionPanel,
    EASYSECTION_PT_SectionModifiers,
    EASYSECTION_UL_SlicerStates,
    EASYSECTION_UL_Elevations,
    EASYSECTION_PT_ElevationPanel,
    EASYSECTION_OT_interactive_arrow,
    SECTION_PT_customization,
]

def purge_properties():
    props_to_remove = [
        (bpy.types.Scene, 'easy_section_props'),
        (bpy.types.Scene, 'es_new_preview_name'),
        (bpy.types.Object, 'es_use_crease'),
        (bpy.types.Object, 'es_crease_angle'),
        (bpy.types.Collection, 'es_link_offsets'),
        (bpy.types.Collection, 'es_group_offset'),
        (bpy.types.Scene, 'es_slicer_states'),
        (bpy.types.Scene, 'es_slicer_states_index'),
        (bpy.types.Scene, 'es_new_state_name'),
        (bpy.types.Scene, 'es_sel_apply_mat'),
        (bpy.types.Scene, 'es_sel_cap_mat'),
        (bpy.types.Scene, 'es_sel_apply_hatch'),
        (bpy.types.Scene, 'es_sel_hatch_type'),
        (bpy.types.Scene, 'es_sel_apply_out_en'),
        (bpy.types.Scene, 'es_sel_outline_enable'),
        (bpy.types.Scene, 'es_sel_apply_out_th'),
        (bpy.types.Scene, 'es_sel_outline_thickness'),
        (bpy.types.Scene, 'es_sel_apply_out_col'),
        (bpy.types.Scene, 'es_sel_outline_color'),
        (bpy.types.Object, 'es_fill_type'),
        (bpy.types.Object, 'es_hatch_scale'),
        (bpy.types.Object, 'es_fade'),
        (bpy.types.Object, 'es_min_dist'),
        (bpy.types.Object, 'es_max_dist')
    ]
    for base, prop_name in props_to_remove:
        if hasattr(base, prop_name):
            try: delattr(base, prop_name)
            except Exception: pass

def get_elevation_name(self):
    return self.name

def set_elevation_name(self, value):
    if not value or value == self.name:
        return
    self.name = value
    final_name = self.name 
    for obj in self.objects:
        if "es_elevation_group" in obj:
            obj["es_elevation_group"] = final_name

@bpy.app.handlers.persistent
def es_sync_selection(scene, depsgraph):
    try:
        obj = bpy.context.active_object
        if obj and "es_elevation_group" in obj:
            grp_name = obj["es_elevation_group"]
            col = bpy.data.collections.get("ES_Previews")
            if col:
                for i, child in enumerate(col.children):
                    if child.name == grp_name and scene.es_elevation_index != i:
                        scene.es_elevation_index = i
                        break
    except:
        pass

@persistent
def es_load_post_handler(dummy):
    def restart_gizmo():
        if not any(getattr(scene, "easysection_is_running", False) for scene in bpy.data.scenes):
            return None

        global gizmo_storage, last_transforms, idle_ticks
        gizmo_storage = GizmoData()
        last_transforms = None
        idle_ticks = 0

        override_context = None
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    for region in area.regions:
                        if region.type == 'WINDOW':
                            override_context = {
                                'window': window,
                                'screen': window.screen,
                                'area': area,
                                'region': region
                            }
                            break
                    if override_context: break
            if override_context: break

        if override_context:
            with bpy.context.temp_override(**override_context):
                try:
                    bpy.ops.easysection.interactive_arrow('INVOKE_DEFAULT')
                except Exception as e:
                    print(f"EasySection Auto-Restart Error: {e}")
                    
        return None

    bpy.app.timers.register(restart_gizmo, first_interval=1.0)


def register_properties_and_handlers():
    bpy.types.Collection.es_link_offsets = bpy.props.BoolProperty(name="Link Offsets", default=True)
    bpy.types.Collection.es_group_offset = bpy.props.FloatProperty(name="Group Offset", default=0.5, update=logic.update_collection_offset)
    
    bpy.types.Scene.es_slicer_states = bpy.props.CollectionProperty(type=EasySectionState)
    bpy.types.Scene.es_slicer_states_index = bpy.props.IntProperty()
    bpy.types.Scene.es_new_state_name = bpy.props.StringProperty(name="State Name", default="Position 1")
    
    bpy.types.Scene.es_sel_apply_mat = bpy.props.BoolProperty(name="Apply", default=False)
    bpy.types.Scene.es_sel_cap_mat = bpy.props.PointerProperty(name="Material", type=bpy.types.Material)
    
    bpy.types.Scene.es_sel_apply_hatch = bpy.props.BoolProperty(name="Apply", default=False)
    bpy.types.Scene.es_sel_hatch_type = bpy.props.EnumProperty(
        name="Hatch",
        items=[
            ('-1', "None", ""), ('0', "Solid", ""), ('1', "RC 01", ""), ('2', "RC 02", ""),
            ('3', "Concrete", ""), ('4', "Brick Wall", ""), ('5', "Brick Front", ""),
            ('6', "Sand", ""), ('7', "Stone", ""), ('8', "Earth", ""),
            ('9', "Gravel", ""), ('10', "Wood", ""), ('11', "Honey", ""), ('12', "Line", "")
        ],
        default='-1'
    )
    bpy.types.Scene.es_show_slicer_views = bpy.props.BoolProperty(name="Show Slicer Views", default=False)
    bpy.types.Scene.es_sel_apply_out_en = bpy.props.BoolProperty(name="Apply", default=False)
    bpy.types.Scene.es_sel_outline_enable = bpy.props.BoolProperty(name="Outline", default=False)
    
    bpy.types.Scene.es_sel_apply_out_th = bpy.props.BoolProperty(name="Apply", default=False)
    bpy.types.Scene.es_sel_outline_thickness = bpy.props.FloatProperty(name="Thickness", default=1.0, min=0.0)
    
    bpy.types.Scene.es_sel_apply_out_col = bpy.props.BoolProperty(name="Apply", default=False)
    bpy.types.Scene.es_sel_outline_color = bpy.props.FloatVectorProperty(name="Color", subtype='COLOR', default=(0,0,0,1), size=4, min=0.0, max=1.0)
    
    bpy.types.Object.es_fill_type = bpy.props.EnumProperty(
        name="Fill Type",
        items=[('0', "Material", ""), ('1', "Solid", ""), ('2', "Hatch", "")],
        default='0',
        update=logic.update_fill_type
    )
    bpy.types.Object.es_hatch_scale = bpy.props.FloatProperty(name="Hatch Scale", default=1.0, min=0.01, update=logic.update_hatch_scale)
    bpy.types.Object.es_fade = bpy.props.FloatProperty(name="Fade", default=80.0, min=0.0, max=100.0, update=logic.update_fade)
    bpy.types.Object.es_min_dist = bpy.props.FloatProperty(name="Min Distance", default=0.0, update=logic.update_min_dist)
    bpy.types.Object.es_max_dist = bpy.props.FloatProperty(name="Max Distance", default=100.0, update=logic.update_max_dist)
    bpy.types.Object.es_use_crease = bpy.props.BoolProperty(name="Use Crease", default=True, update=logic.update_use_crease)

    bpy.types.Scene.easy_section_props = bpy.props.PointerProperty(type=EasySectionProperties)
    bpy.types.Scene.es_new_preview_name = bpy.props.StringProperty(name="New Name", default="Elevation")
    bpy.types.Collection.es_elevation_name = bpy.props.StringProperty(
        get=get_elevation_name,
        set=set_elevation_name
    )
    bpy.types.Scene.es_slicer_states_index = bpy.props.IntProperty(update=logic.update_slicer_index)
    bpy.types.Scene.es_elevation_index = bpy.props.IntProperty(update=logic.update_elevation_index)
    bpy.types.Object.es_crease_angle = bpy.props.FloatProperty(name="Crease Angle", default=100.0, min=0.0, max=180.0, update=logic.update_crease)
    
    bpy.types.Scene.es_show_slicer_views = bpy.props.BoolProperty(name="Show Slicer Views", default=False)
    bpy.types.Scene.es_show_live_mods = bpy.props.BoolProperty(name="Show Live Modifiers", default=False)
    bpy.types.Scene.es_show_selected_setup = bpy.props.BoolProperty(name="Show Selected Setup", default=False)
    
    bpy.types.Collection.es_link_depth = bpy.props.BoolProperty(name="Link Depth", default=True)
    bpy.types.Collection.es_global_fade = bpy.props.FloatProperty(name="Fade", default=80.0, min=0.0, max=100.0, update=logic.update_global_depth)
    bpy.types.Collection.es_global_min = bpy.props.FloatProperty(name="Min Dist", default=0.0, update=logic.update_global_depth)
    bpy.types.Collection.es_global_max = bpy.props.FloatProperty(name="Max Dist", default=100.0, update=logic.update_global_depth)
    
    bpy.types.Scene.easysection_is_running = bpy.props.BoolProperty(default=False)
    bpy.types.Scene.easysection_use_occlusion = bpy.props.BoolProperty(name="Occlusion", default=True)
    bpy.types.Scene.easysection_update_interval = bpy.props.FloatProperty(name="Refresh", default=0.01, min=0.01, max=2.0)
    bpy.types.Scene.easysection_drag_sensitivity = bpy.props.FloatProperty(name="Speed", default=0.005, min=0.001, max=0.1)
    bpy.types.Scene.easysection_arrow_size = bpy.props.FloatProperty(name="Scale", default=8.0, min=1.0, max=100.0)
    bpy.types.Scene.easysection_use_undo = bpy.props.BoolProperty(name="Undo", default=True)
    bpy.types.Scene.easysection_arrow_color = bpy.props.FloatVectorProperty(name="Color", subtype='COLOR', size=4, default=(1.0, 0.5, 0.0, 1.0))
    bpy.types.Scene.dha_sync_active = bpy.props.BoolProperty(name="Realtime Mode", default=False, update=update_realtime_mode)
    bpy.types.Scene.dha_wire_mode = bpy.props.EnumProperty(name="Wire Style", items=[('0', "Grease Pencil", ""), ('1', "Wire", ""), ('2', "Off", "")], default='0', update=update_wire)
    bpy.types.Scene.dha_wire_color = bpy.props.FloatVectorProperty(name="Wire Color", subtype='COLOR', size=4, min=0.0, max=1.0, default=(1.0, 1.0, 1.0, 1.0), update=update_wire)
    bpy.types.Scene.dha_wire_slider = bpy.props.FloatProperty(name="Wire Value", min=0.0, max=1000.0, default=1.0, update=update_wire)
    
    bpy.types.Scene.hatch_coord_mode = bpy.props.EnumProperty(
        items=[
            ('UV', 'UV', 'Use UV Coordinates', 'UV', 0),
            ('Camera', 'Camera', 'Use Camera Coordinates', 'CAMERA_DATA', 1)
        ],
        name="Hatch Mode",
        default='Camera',
        update=update_hatch_coordinates
    )

    bpy.types.Scene.es_rel_lines_state = bpy.props.BoolProperty(default=True)

    bpy.app.timers.register(check_for_updates, persistent=True)
    bpy.app.handlers.undo_pre.append(undo_pre_handler)
    bpy.app.handlers.undo_post.append(undo_post_handler)
    
    if es_sync_selection not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(es_sync_selection)

    if es_load_post_handler not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(es_load_post_handler)

def unregister_properties_and_handlers():
    purge_properties()
    
    if bpy.app.timers.is_registered(check_for_updates):
        bpy.app.timers.unregister(check_for_updates)
        
    try: bpy.app.handlers.undo_pre.remove(undo_pre_handler)
    except: pass
    
    try: bpy.app.handlers.undo_post.remove(undo_post_handler)
    except: pass
    
    if es_sync_selection in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(es_sync_selection)

    if es_load_post_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(es_load_post_handler)