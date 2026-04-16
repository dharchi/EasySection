import bpy
import os
import sys
import mathutils
import bmesh
import math
import ezdxf
import json





# تحديد مسار ملف الأصول (Assets) تلقائياً
BASE_DIR = os.path.dirname(__file__)
ASSETS_PATH = os.path.join(BASE_DIR, "assets", "easy_section_assets.blend")

# ==========================================
# 1. ثوابت القطاعات (Section Logic)
# ==========================================
COL_NAME = "DHAEasySection"
SLICER_NAME = "DHASlicerBox"
SHADER_NAME = "DHATheEasyBooleaner"
GEO_SYNC = "DHASlicerSync"
GEO_SINGLE = "DHAFurnitureSingleHide"
GEO_PARENT = "DHAFurnitureParentHide"
SCRIPT_NAME = "DHAFixedScale"
HIDDEN_COLS = ["DHAArrowSettings", "DHAArrowsStyle"]

HATCH_DICT = {
    0:  {'name': 'SOLID',      'scale': 1.0,    'layer': 'ES_HATCH_SOLID',    'color': 8},
    1:  {'name': 'ANSI37',     'scale': 0.03,   'layer': 'ES_HATCH_RC1',      'color': 1},  
    2:  {'name': 'ANSI31',     'scale': 0.03,   'layer': 'ES_HATCH_RC2',      'color': 2},  
    3:  {'name': 'AR-CONC',    'scale': 0.002,  'layer': 'ES_HATCH_CONCRETE', 'color': 3},  
    4:  {'name': 'ANSI32',     'scale': 0.02,   'layer': 'ES_HATCH_BRICK',    'color': 4},  
    5:  {'name': 'AR-B816',    'scale': 0.001,  'layer': 'ES_HATCH_BRICK_F',  'color': 5},  
    6:  {'name': 'AR-SAND',    'scale': 0.002,  'layer': 'ES_HATCH_SAND',     'color': 6},  
    7:  {'name': 'ANSI33',     'scale': 0.02,   'layer': 'ES_HATCH_STONE',    'color': 7},  
    8:  {'name': 'EARTH',      'scale': 0.05,   'layer': 'ES_HATCH_EARTH',    'color': 9},  
    9:  {'name': 'GRAVEL',     'scale': 0.008,  'layer': 'ES_HATCH_GRAVEL',   'color': 8},  
    10: {'name': 'GOST_WOOD',  'scale': 0.025,  'layer': 'ES_HATCH_WOOD',     'color': 34}, 
    11: {'name': 'HONEY',      'scale': 0.025,  'layer': 'ES_HATCH_HONEY',    'color': 40}, 
    12: {'name': 'LINE',       'scale': 0.025,  'layer': 'ES_HATCH_LINE',     'color': 7},  
}



def set_geo_input(obj, mod_name, input_name, value):
    mod = obj.modifiers.get(mod_name)
    if mod and mod.type == 'NODES' and mod.node_group:
        for item in mod.node_group.interface.items_tree:
            if getattr(item, 'in_out', None) == 'INPUT' and item.name == input_name:
                if value is None:
                    if item.identifier in mod: del mod[item.identifier]
                else:
                    mod[item.identifier] = value
                mod.show_viewport = mod.show_viewport
                break

def toggle_relationship_lines(context, show=False):
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            for space in area.spaces:
                if space.type == 'VIEW_3D':
                    space.overlay.show_relationship_lines = show

def exclude_collection(layer_collection, col_name):
    if layer_collection.name == col_name:
        layer_collection.exclude = True
        return True
    for child in layer_collection.children:
        if exclude_collection(child, col_name): return True
    return False

def process_sync_logic(operator, context, props, is_update=False):
    slicer_obj = bpy.data.objects.get(SLICER_NAME)
    if not slicer_obj:
        operator.report({'ERROR'}, "لم يتم العثور على SlicerBox، يرجى عمل Apply أولاً.")
        return False

    # 1. معالجة الـ Section Collection
    section_objs = [obj for obj in props.section_collection.all_objects if obj.type == 'MESH' and obj.name != SLICER_NAME]
    if section_objs:
        if not is_update:
            min_c = mathutils.Vector((float('inf'), float('inf'), float('inf')))
            max_c = mathutils.Vector((float('-inf'), float('-inf'), float('-inf')))
            for obj in section_objs:
                for corner in obj.bound_box:
                    w_corner = obj.matrix_world @ mathutils.Vector(corner)
                    for i in range(3):
                        min_c[i] = min(min_c[i], w_corner[i])
                        max_c[i] = max(max_c[i], w_corner[i])
            slicer_obj.scale = (1, 1, 1)
            slicer_obj.location = (min_c + max_c) / 2
            slicer_obj.dimensions = (max_c - min_c) + mathutils.Vector((props.offset*2, props.offset*2, props.offset*2))
            # --- التعديل الجديد: نسخ نفس الأبعاد والمكان للـ Proxy ---
            proxy_obj = bpy.data.objects.get("DHASlicerProxy")
            if proxy_obj:
                proxy_obj.scale = (1, 1, 1)
                proxy_obj.location = slicer_obj.location.copy()
                proxy_obj.dimensions = slicer_obj.dimensions.copy()
            # -------------------------------------------------------
            # =======================================================
            # التعديل الجديد: ضبط إعدادات الكاميرات بناءً على أكبر بُعد للقطاع
            # =======================================================
            # 1. نجيب أكبر قيمة في الـ (X, Y, Z) للـ SlicerBox
            calculated_dim = (max_c - min_c) + mathutils.Vector((props.offset*2, props.offset*2, props.offset*2))
            max_dim = max(calculated_dim.x, calculated_dim.y, calculated_dim.z)
            # 2. نضربها في 2.5 زي ما طلبت
            cam_target_val = max_dim * 2.5

            # 3. قايمة الكاميرات الأساسية (وضفنا الكاميرات المعكوسة بحرف N)
            base_cams = [
                "DHABackwardCam", "DHADownCam", "DHAForwardCam", 
                "DHALeftCam", "DHARightCam", "DHAUpCam"
            ]
            cams_to_update = base_cams + [name + "N" for name in base_cams]

            # 4. نلف عليهم ونطبق القيم
            for cam_name in cams_to_update:
                cam_obj = bpy.data.objects.get(cam_name)
                # نتأكد إن الأوبجكت موجود ونوعه كاميرا عشان ميعملش إيرور
                if cam_obj and cam_obj.type == 'CAMERA' and cam_obj.data:
                    # نتأكد إنها مضبوطة على Orthographic
                    cam_obj.data.type = 'ORTHO' 
                    # نطبق القيم
                    cam_obj.data.ortho_scale = cam_target_val
                    cam_obj.data.clip_end = cam_target_val
            # =======================================================
            
            
        geo_sync_group = bpy.data.node_groups.get(GEO_SYNC)
        shader_group = bpy.data.node_groups.get(SHADER_NAME)

        for obj in section_objs:
            if geo_sync_group and hasattr(obj, "modifiers"):
                mod = obj.modifiers.get(GEO_SYNC) or obj.modifiers.new(GEO_SYNC, 'NODES')
                mod.node_group = geo_sync_group
                if hasattr(mod, "use_pin_to_last"):
                    mod.use_pin_to_last = True
            
            if shader_group:
                if hasattr(obj.data, "materials") and len(obj.data.materials) == 0:
                    default_mat = bpy.data.materials.get("Material") or bpy.data.materials.new(name="Material")
                    obj.data.materials.append(default_mat)

                if hasattr(obj, "material_slots"):
                    for slot in obj.material_slots:
                        mat = slot.material
                        if not mat: continue
                        mat.use_nodes = True
                        nodes = mat.node_tree.nodes
                        links = mat.node_tree.links
                        if any(n.type == 'GROUP' and n.node_tree and n.node_tree.name == SHADER_NAME for n in nodes): continue
                        out_node = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None)
                        if out_node:
                            surface_in = out_node.inputs.get('Surface')
                            if surface_in:
                                old_sock = surface_in.links[0].from_socket if surface_in.is_linked else None
                                new_node = nodes.new('ShaderNodeGroup')
                                new_node.node_tree = shader_group
                                new_node.location = (out_node.location.x - 300, out_node.location.y)
                                if old_sock: links.new(old_sock, new_node.inputs[0])
                                links.new(new_node.outputs[0], surface_in)

    # 2. معالجة الـ Object Collection (الفرش)
    # 2. معالجة الـ Object Collection (الفرش)
    if props.object_collection:
        obj_col = props.object_collection
        # --- التعديل الجديد: منع تقاطع خطوط الفرش ---
        if hasattr(obj_col, "lineart_usage"):
            obj_col.lineart_usage = 'NO_INTERSECTION'
        processed_roots = set()
        
        def get_all_descendants(parent, desc_list):
            for child in parent.children:
                desc_list.append(child)
                get_all_descendants(child, desc_list)

        valid_objects = [o for o in obj_col.all_objects if not o.name.endswith("_controller")]

        for obj in valid_objects:
            if not hasattr(obj, "modifiers") or obj.type not in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'VOLUME'}:
                continue

            if not obj.parent and not obj.children:
                single_grp = bpy.data.node_groups.get(GEO_SINGLE)
                if single_grp:
                    mod = obj.modifiers.get(GEO_SINGLE) or obj.modifiers.new(GEO_SINGLE, 'NODES')
                    mod.node_group = single_grp
                    if hasattr(mod, "use_pin_to_last"):
                        mod.use_pin_to_last = True
            else:
                root = obj
                while root.parent: root = root.parent
                if root in processed_roots: continue
                processed_roots.add(root)

                hierarchy_list = [root]
                get_all_descendants(root, hierarchy_list)

                # --- التعديل هنا لحل مشكلة الـ Update والـ Nested Collections ---
                # تحديد الكوليكشن الحالي للأوبجكت (الأب الفعلي)
                current_parent_col = root.users_collection[0] if root.users_collection else obj_col
                new_col_name = root.name
                
                if new_col_name not in bpy.data.collections:
                    new_col = bpy.data.collections.new(new_col_name)
                    # نربطه بالأب فقط لو مش هو نفسه (عشان ميحصلش RuntimeError)
                    if new_col != current_parent_col:
                        current_parent_col.children.link(new_col)
                else:
                    new_col = bpy.data.collections[new_col_name]
                    # لو موجود، نتأكد إنه مربوط بالأب الصح ومش مربوط بنفسه
                    if new_col != current_parent_col and new_col.name not in current_parent_col.children.keys():
                        current_parent_col.children.link(new_col)

                exclude_collection(context.view_layer.layer_collection, new_col_name)

                for h_obj in hierarchy_list:
                    if h_obj.name not in new_col.objects: 
                        new_col.objects.link(h_obj)
                    
                    for col in list(h_obj.users_collection):
                        if col != new_col:
                            # نفك الارتباط فقط لو الكوليكشن ده تبع نظام الإضافة
                            if col == obj_col or col.name in [c.name for c in obj_col.children_recursive]:
                                if len(h_obj.users_collection) > 1:
                                    col.objects.unlink(h_obj)

                # --- تكملة الجزء الخاص بالكنترولر ---
                ctrl_name = f"{root.name}_controller"
                ctrl_obj = bpy.data.objects.get(ctrl_name)
                if not ctrl_obj:
                    mesh_data = bpy.data.meshes.new(f"{ctrl_name}_mesh")
                    ctrl_obj = bpy.data.objects.new(ctrl_name, mesh_data)
                    import bmesh
                    bm = bmesh.new()
                    bmesh.ops.create_cube(bm, size=0.5)
                    bm.to_mesh(mesh_data)
                    bm.free()
                    ctrl_obj.location = (0, 0, 0)
                    ctrl_obj.display_type = 'TEXTURED'
                    # نربط الكنترولر بنفس الكوليكشن الأب اللي كان فيه الأوبجكت
                    current_parent_col.objects.link(ctrl_obj)

                parent_grp = bpy.data.node_groups.get(GEO_PARENT)
                if parent_grp:
                    mod = ctrl_obj.modifiers.get(GEO_PARENT) or ctrl_obj.modifiers.new(GEO_PARENT, 'NODES')
                    mod.node_group = parent_grp
                    if hasattr(mod, "use_pin_to_last"):
                        mod.use_pin_to_last = True
                    for item in mod.node_group.interface.items_tree:
                        if getattr(item, 'in_out', None) == 'INPUT' and item.name == "Parent&Children Collection":
                            mod[item.identifier] = new_col
                            break

    # 1. حفظ الحالة الحالية قبل الإطفاء
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            for space in area.spaces:
                if space.type == 'VIEW_3D':
                    context.scene.es_rel_lines_state = space.overlay.show_relationship_lines
                    break # يكفي نجيب حالة أول فيوبورت نلاقيه
    
    toggle_relationship_lines(context, show=False)
    return True

def process_remove_logic(props, context):
    if props.section_collection:
        for obj in props.section_collection.all_objects:
            if obj.type == 'MESH':
                mod = obj.modifiers.get(GEO_SYNC)
                if mod: obj.modifiers.remove(mod)
                for slot in obj.material_slots:
                    mat = slot.material
                    if mat and mat.use_nodes:
                        nodes = mat.node_tree.nodes
                        links = mat.node_tree.links
                        group = next((n for n in nodes if n.type == 'GROUP' and n.node_tree and n.node_tree.name == SHADER_NAME), None)
                        out_node = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None)
                        if group and out_node:
                            surface_in = out_node.inputs.get('Surface')
                            if surface_in and group.inputs[0].is_linked:
                                src = group.inputs[0].links[0].from_socket
                                links.new(src, surface_in)
                            nodes.remove(group)

    if props.object_collection:
        obj_col = props.object_collection
        controllers = [obj for obj in obj_col.all_objects if obj.name.endswith("_controller")]
        
        for ctrl in controllers:
            base_name = ctrl.name.replace("_controller", "")
            sub_col = bpy.data.collections.get(base_name)
            
            if sub_col:
                # 1. البحث عن الكوليكشن الأب الفعلي الذي يحتوي على sub_col
                # نبحث في الكوليكشن الرئيسي وكل ذريته (Recursive)
                parent_col = None
                search_list = [obj_col] + list(obj_col.children_recursive)
                
                for p in search_list:
                    if sub_col.name in p.children.keys():
                        parent_col = p
                        break
                
                # 2. لو لقينا الأب (حتى لو في مستوى عميق) نرجع العناصر له ونحذف الصب كوليكشن
                if parent_col:
                    for obj in list(sub_col.objects):
                        if obj.name not in parent_col.objects:
                            parent_col.objects.link(obj)
                    bpy.data.collections.remove(sub_col)
            
            # 3. حذف الكونترولر في كل الأحوال
            bpy.data.objects.remove(ctrl, do_unlink=True)
            
        for obj in obj_col.all_objects:
            for m in [GEO_SINGLE, GEO_PARENT]:
                mod = obj.modifiers.get(m)
                if mod: obj.modifiers.remove(mod)

    # -------------------------------------------------------------------------
    # الجزء الخاص بالمسح الجذري (يوضع في نهاية دالة process_remove_logic)
    # -------------------------------------------------------------------------

    # --- دالة مساعدة لمسح الكوليكشن وكل ما بداخلها من جذورها ---
    def delete_collection_tree(col):
        # 1. مسح الكوليكشنز الفرعية أولاً (Recursion)
        for child in list(col.children):
            delete_collection_tree(child)
            
        # 2. مسح كل الأوبجكتس اللي جوه الكوليكشن دي نهائياً
        for obj in list(col.objects):
            if obj.name in bpy.data.objects:
                bpy.data.objects.remove(obj, do_unlink=True)
                
        # 3. فك الارتباط من أي مشهد
        for scene in bpy.data.scenes:
            if col.name in scene.collection.children:
                scene.collection.children.unlink(col)
                
        # 4. مسح الكوليكشن نفسها من الذاكرة
        if col.name in bpy.data.collections:
            bpy.data.collections.remove(col)

    # 1. تطبيق المسح على الكوليكشن الرئيسية اللي طلبتها
    main_col = bpy.data.collections.get(COL_NAME) # اللي هي DHAEasySection
    if main_col:
        delete_collection_tree(main_col)

    # تطبيق المسح على الكوليكشنز المخفية بالمرة (احتياطياً لو كانوا برا الأساسية)
    for hc in HIDDEN_COLS:
        hidden_col = bpy.data.collections.get(hc)
        if hidden_col:
            delete_collection_tree(hidden_col)

    # 2. مسح النودز اللي إنت حددتها بالاسم بالإضافة للشيدر الأساسي
    nodes_to_remove = [
        "DHASlicerSync",
        "DHAFurnitureParentHide",
        "DHAArrowInstance",
        "DHAArrowType",
        "DHAFurnitureSingleHide",
        "DHALineArt",
        "IntersectionBoolean",
        "BooleanSwitch",
        "DifferenceBoolean",
        "DHA-Boolean",
        "FixNormal",
        "DHASlicerBox",
        "DHASlicerProxy",
        SHADER_NAME # (DHATheEasyBooleaner) من ثوابت الكود بتاعك
    ]

    for ng_name in nodes_to_remove:
        ng = bpy.data.node_groups.get(ng_name)
        if ng:
            bpy.data.node_groups.remove(ng)

    # 4. مسح الماتيريالز الخاصة بالسكشن والهاتش نهائياً من الملف
    mats_to_remove = [
        "DHA SectionFill",
        "Hatch_ANSI31",
        "Hatch_ANSI32",
        "Hatch_ANSI33",
        "Hatch_ANSI37",
        "Hatch_AR-B816",
        "Hatch_AR-CONC",
        "Hatch_AR-SAND",
        "Hatch_EARTH",
        "Hatch_GOST-WOOD",
        "Hatch_GRAVEL",
        "Hatch_HONEY",
        "Hatch_LINE",
        "MABlack"
    ]
    
    for mat_name in mats_to_remove:
        mat = bpy.data.materials.get(mat_name)
        if mat:
            bpy.data.materials.remove(mat, do_unlink=True)

    # 3. إرجاع خطوط العلاقات للظهور
    

    # 2. مسح الـ Node Groups نهائياً عشان متحتفظش بأي ريفرينس يخلي أوبجكت يعيش
    nodes_to_remove = [SHADER_NAME, GEO_SYNC, GEO_SINGLE, GEO_PARENT, "DHALineArt"]
    for ng_name in nodes_to_remove:
        ng = bpy.data.node_groups.get(ng_name)
        if ng:
            bpy.data.node_groups.remove(ng)

    toggle_relationship_lines(context, show=context.scene.es_rel_lines_state)

def update_global_outline(self, context):
    if not self.section_collection: return
    for obj in self.section_collection.all_objects:
        if obj.type == 'MESH':
            gn = obj.modifiers.get(GEO_SYNC)
            if gn and "Socket_16" in gn:
                gn["Socket_16"] = self.outline_enable
                gn.show_viewport = gn.show_viewport

def update_global_outline_thickness(self, context):
    if not self.section_collection: return
    for obj in self.section_collection.all_objects:
        if obj.type == 'MESH':
            gn = obj.modifiers.get(GEO_SYNC)
            if gn and "Socket_14" in gn:
                gn["Socket_14"] = self.outline_thickness
                gn.show_viewport = gn.show_viewport

def update_global_outline_color(self, context):
    if not self.section_collection: return
    for obj in self.section_collection.all_objects:
        if obj.type == 'MESH':
            gn = obj.modifiers.get(GEO_SYNC)
            if gn and "Socket_15" in gn:
                gn["Socket_15"] = self.outline_color
                gn.show_viewport = gn.show_viewport

def get_slicer_state():
    objs = ["DHASlicerBox", "DHABackwardArrow", "DHADownArrow", "DHAForwardArrow", "DHALeftArrow", "DHARightArrow", "DHAUpArrow"]
    state_data = {}
    for name in objs:
        obj = bpy.data.objects.get(name)
        if obj:
            state_data[name] = {
                "loc": list(obj.location),
                "rot": list(obj.rotation_euler),
                "quat": list(obj.rotation_quaternion),
                "scale": list(obj.scale),
                "rot_mode": obj.rotation_mode
            }
    return json.dumps(state_data) if state_data else None

def restore_slicer_state(data_str):
    try:
        state_data = json.loads(data_str)
        for name, data in state_data.items():
            obj = bpy.data.objects.get(name)
            if obj:
                obj.location = data["loc"]
                obj.rotation_mode = data["rot_mode"]
                if obj.rotation_mode == 'QUATERNION':
                    obj.rotation_quaternion = data["quat"]
                else:
                    obj.rotation_euler = data["rot"]
                obj.scale = data["scale"]
    except: pass

def update_slicer_index(self, context):
    idx = self.es_slicer_states_index
    if 0 <= idx < len(self.es_slicer_states):
        restore_slicer_state(self.es_slicer_states[idx].state_data)

def update_elevation_index(self, context):
    col = bpy.data.collections.get("ES_Previews")
    if col and 0 <= self.es_elevation_index < len(col.children):
        grp_name = col.children[self.es_elevation_index].name
        grp_col = bpy.data.collections.get(grp_name)
        if grp_col:
            bpy.ops.object.select_all(action='DESELECT')
            objs = list(grp_col.objects)
            visible_objs = [o for o in objs if not o.hide_viewport]
            for obj in visible_objs: obj.select_set(True)
            if visible_objs: context.view_layer.objects.active = visible_objs[0]            

def update_crease(self, context):
    lart = self.modifiers.get("Line Art")
    if lart: lart.crease_threshold = math.radians(self.es_crease_angle)

def update_use_crease(self, context):
    lart = self.modifiers.get("Line Art")
    if lart: 
        lart.use_crease = self.es_use_crease

def update_collection_offset(self, context):
    if not getattr(self, "es_link_offsets", False): return
    for obj in self.objects:
        gn = obj.modifiers.get("DHALineArt")
        if gn:
            if "Socket_2" in gn: gn["Socket_2"] = self.es_group_offset
            gn.show_viewport = gn.show_viewport

# أضف هذه الدالة في ملف logic.py
def update_global_depth(self, context):
    if not getattr(self, "es_link_depth", False): return
    for obj in self.objects:
        gn = obj.modifiers.get("DHALineArt")
        if gn:
            if "Socket_14" in gn: gn["Socket_14"] = self.es_global_fade
            if "Socket_15" in gn: gn["Socket_15"] = self.es_global_min
            if "Socket_16" in gn: gn["Socket_16"] = self.es_global_max
            gn.show_viewport = gn.show_viewport

def update_fill_type(self, context):
    gn = self.modifiers.get("DHALineArt")
    if gn and "Socket_12" in gn:
        gn["Socket_12"] = int(self.es_fill_type)
        gn.show_viewport = gn.show_viewport

def update_hatch_scale(self, context):
    gn = self.modifiers.get("DHALineArt")
    if gn and "Socket_10" in gn:
        gn["Socket_10"] = self.es_hatch_scale
        gn.show_viewport = gn.show_viewport

def update_fade(self, context):
    gn = self.modifiers.get("DHALineArt")
    if gn and "Socket_14" in gn:
        gn["Socket_14"] = self.es_fade
        gn.show_viewport = gn.show_viewport

def update_min_dist(self, context):
    gn = self.modifiers.get("DHALineArt")
    if gn and "Socket_15" in gn:
        gn["Socket_15"] = self.es_min_dist
        gn.show_viewport = gn.show_viewport

def update_max_dist(self, context):
    gn = self.modifiers.get("DHALineArt")
    if gn and "Socket_16" in gn:
        gn["Socket_16"] = self.es_max_dist
        gn.show_viewport = gn.show_viewport

def clean_orphan_data():
    pass

def delete_preview_group(grp_name):
    grp_col = bpy.data.collections.get(grp_name)
    if grp_col:
        for obj in list(grp_col.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.collections.remove(grp_col)
    clean_orphan_data()

def create_single_gp(base_name, suffix, target_col, cam_obj, parent_col):
    gp_name = f"{base_name}{suffix}"
    gp_data = bpy.data.grease_pencils.new(f"{gp_name}_Data")
    gp_data.layers.new("Layer")
    gp_obj = bpy.data.objects.new(gp_name, gp_data) 
    
    parent_col.objects.link(gp_obj)
    gp_obj["es_is_preview"] = 1
    gp_obj["es_elevation_group"] = base_name
    gp_obj["es_preview_type"] = suffix
    gp_obj["es_cam_name"] = cam_obj.name
    gp_obj.location = (0, 0, 0)
    gp_obj.es_crease_angle = 100.0
    gp_obj.es_fade = 80.0
    gp_obj.es_min_dist = 0.0
    gp_obj.es_max_dist = 100.0

    mat = bpy.data.materials.get("MABlack")
    if not mat:
        mat = bpy.data.materials.new("MABlack")
        mat.diffuse_color = (0, 0, 0, 1)

    if mat.name not in gp_obj.data.materials: gp_obj.data.materials.append(mat)

    lart = gp_obj.modifiers.new(name="Line Art", type='LINEART')
    lart.source_type = 'COLLECTION'
    lart.source_collection = target_col
    lart.target_layer = "Layer"
    lart.target_material = mat 
    lart.use_custom_camera = True
    lart.source_camera = cam_obj
    lart.radius = 0.01
    lart.crease_threshold = math.radians(100.0) 
    
    # 1. تصفير الـ stroke_depth_offset بشكل عام للكائنين لضمان دقة الخطوط
    lart.stroke_depth_offset = 0.0 

    if hasattr(lart, "use_back_face_culling"):
        lart.use_back_face_culling = True

    if suffix == "_Obj":
        # تم استخدام use_intersection كما طلبت بالضبط لضمان الإغلاق
        if hasattr(lart, "use_intersection"):
            lart.use_intersection = False
        
        # 2. إيقاف الـ Crease افتراضياً للفرش (_Obj)
        lart.use_crease = False
        gp_obj.es_use_crease = False
    elif suffix == "_Ele":
        # تشغيل الـ Crease افتراضياً للواجهة (_Ele)
        lart.use_crease = True
        gp_obj.es_use_crease = True

    if hasattr(lart, 'use_occlusion'): lart.use_occlusion = True

    bpy.context.view_layer.update()

    gn_group = bpy.data.node_groups.get("DHALineArt")
    if gn_group:
        gn_mod = gp_obj.modifiers.new(name="DHALineArt", type='NODES')
        gn_mod.node_group = gn_group
        if "Socket_3" in gn_mod: gn_mod["Socket_3"] = cam_obj
        if "Socket_2" in gn_mod: gn_mod["Socket_2"] = 0.5
        if "Socket_14" in gn_mod: gn_mod["Socket_14"] = 80.0
        if "Socket_15" in gn_mod: gn_mod["Socket_15"] = 0.0
        if "Socket_16" in gn_mod: gn_mod["Socket_16"] = 100.0
        bpy.context.view_layer.update()
            
    return gp_obj

def create_single_mesh_cut(base_name, target_col, cam_obj, parent_col):
    mesh_name = f"{base_name}_Cut"
    mesh_data = bpy.data.meshes.new(f"{mesh_name}_Data")
    mesh_data.from_pydata([(0, 0, 0)], [], [])
    
    cut_obj = bpy.data.objects.new(mesh_name, mesh_data)
    parent_col.objects.link(cut_obj)
    
    cut_obj["es_is_preview"] = 1
    cut_obj["es_elevation_group"] = base_name
    cut_obj["es_preview_type"] = "_Cut"
    cut_obj["es_cam_name"] = cam_obj.name
    cut_obj.location = (0, 0, 0)
    
    cut_obj.es_fill_type = '0'
    cut_obj.es_hatch_scale = 1.0

    mat = bpy.data.materials.get("MABlack")
    if not mat:
        mat = bpy.data.materials.new("MABlack")
        mat.diffuse_color = (0, 0, 0, 1)

    if mat.name not in cut_obj.data.materials: cut_obj.data.materials.append(mat)

    bpy.context.view_layer.update()

    gn_group = bpy.data.node_groups.get("DHALineArt")
    if gn_group:
        gn_mod = cut_obj.modifiers.new(name="DHALineArt", type='NODES')
        gn_mod.node_group = gn_group
        
        if "Socket_3" in gn_mod: gn_mod["Socket_3"] = cam_obj
        if "Socket_13" in gn_mod: gn_mod["Socket_13"] = target_col
        if "Socket_6" in gn_mod: gn_mod["Socket_6"] = True 
        if "Socket_12" in gn_mod: gn_mod["Socket_12"] = 0 
        if "Socket_10" in gn_mod: gn_mod["Socket_10"] = 1.0
        if "Socket_2" in gn_mod: gn_mod["Socket_2"] = 0.5
        bpy.context.view_layer.update()

    return cut_obj

def create_live_preview_group(name, props):
    fresh_sec_col = bpy.data.collections.get(props.section_collection.name) if props.section_collection else None
    fresh_obj_col = bpy.data.collections.get(props.object_collection.name) if props.object_collection else None

    if not fresh_sec_col and not fresh_obj_col:
        raise Exception("MISSING_ARROW")

    active_obj = bpy.context.active_object
    arrow_to_cam = {
        "DHABackwardArrow": "DHABackwardCam", "DHADownArrow": "DHADownCam",
        "DHAForwardArrow": "DHAForwardCam", "DHALeftArrow": "DHALeftCam",
        "DHARightArrow": "DHARightCam", "DHAUpArrow": "DHAUpCam"
    }
    
    if not active_obj or active_obj.name not in arrow_to_cam:
        raise Exception("MISSING_ARROW")
        
    cam_name = arrow_to_cam[active_obj.name]
    if props.invert_section: cam_name += "N"
        
    cam_obj = bpy.data.objects.get(cam_name)
    if not cam_obj: raise Exception(f"الكاميرا '{cam_name}' غير موجودة!")

    if not bpy.data.node_groups.get("DHALineArt"): 
        raise Exception("يرجى عمل Apply للسكشن أولاً لاستيراد نود DHALineArt!")

    main_col = bpy.data.collections.get(COL_NAME)
    if main_col and main_col.name not in bpy.context.scene.collection.children:
        try: bpy.context.scene.collection.children.link(main_col)
        except: pass

    preview_col = bpy.data.collections.get("ES_Previews")
    if not preview_col:
        preview_col = bpy.data.collections.new("ES_Previews")
        preview_col.color_tag = 'COLOR_06'
        if main_col: main_col.children.link(preview_col)
        else: bpy.context.scene.collection.children.link(preview_col)
    else:
        preview_col.color_tag = 'COLOR_06'
        if main_col and preview_col.name not in main_col.children:
            try: main_col.children.link(preview_col)
            except: pass

    unique_name = name
    counter = 1
    while bpy.data.collections.get(unique_name):
        unique_name = f"{name}.{counter:03d}"
        counter += 1

    grp_col = bpy.data.collections.new(unique_name)
    grp_col.color_tag = 'COLOR_06'
    preview_col.children.link(grp_col)
    grp_col.es_link_offsets = True

    bpy.ops.object.select_all(action='DESELECT')
    
    if fresh_sec_col:
        gp_ele = create_single_gp(unique_name, "_Ele", fresh_sec_col, cam_obj, grp_col)
        gp_ele.select_set(True)
        cut_obj = create_single_mesh_cut(unique_name, fresh_sec_col, cam_obj, grp_col)
        cut_obj.select_set(True)
        bpy.context.view_layer.objects.active = cut_obj
        
    if fresh_obj_col:
        gp_obj = create_single_gp(unique_name, "_Obj", fresh_obj_col, cam_obj, grp_col)
        gp_obj.select_set(True)
        bpy.context.view_layer.objects.active = gp_obj

# ==========================================
# نظام التجميد السليم للواجهات والـ Cut
# ==========================================
def freeze_group(grp_name):
    grp_col = bpy.data.collections.get(grp_name)
    if not grp_col or grp_col.get("es_is_frozen"): return

    state_data = get_slicer_state()
    if state_data: grp_col["es_frozen_slicer_state"] = state_data

    objs = [o for o in grp_col.objects if o.get("es_is_preview") and not o.get("es_is_original_frozen") and not o.get("es_is_frozen_mesh")]

    for original_obj in objs:
        vals = {"Socket_2": 0.5, "Socket_4": (0, 0, 0, 1), "Socket_5": 0.01, "Socket_6": False, 
                "Socket_12": 0, "Socket_10": 1.0, "Socket_14": 80.0, "Socket_15": 0.0, "Socket_16": 100.0}
                
        gn = original_obj.modifiers.get("DHALineArt")
        if gn:
            for k in vals: 
                if k in gn:
                    val = gn[k]
                    if isinstance(val, str): vals[k] = val
                    else:
                        try: vals[k] = tuple(val)
                        except TypeError: vals[k] = val

        cam_name = original_obj.get("es_cam_name")
        preview_type = original_obj.get("es_preview_type")
        is_gp = original_obj.type == 'GREASEPENCIL'
        
        bpy.ops.object.select_all(action='DESELECT')
        original_obj.select_set(True)
        bpy.context.view_layer.objects.active = original_obj
        
        bpy.ops.object.duplicate(linked=False)
        dup_obj = bpy.context.active_object
        
        original_obj.hide_viewport = True
        original_obj.hide_render = True
        original_obj["es_is_original_frozen"] = 1
        
        bpy.ops.object.select_all(action='DESELECT')
        dup_obj.select_set(True)
        bpy.context.view_layer.objects.active = dup_obj
        
        # بنخبز الميش والهاتش كله والـ Socket_6 شغال 
        if is_gp:
            try: bpy.ops.object.lineart_bake_static()
            except: pass
        
        bpy.ops.object.convert(target='MESH')
        mesh_obj = bpy.context.active_object
        
        mesh_obj["es_is_preview"] = 1 
        mesh_obj["es_elevation_group"] = grp_name
        mesh_obj["es_is_frozen_mesh"] = 1
        
        if cam_name: mesh_obj["es_cam_name"] = cam_name
        if preview_type: mesh_obj["es_preview_type"] = preview_type
        
        # بنرجع النودز عشان اللون والسمك يتحكم فيهم المستخدم
        gn_group = bpy.data.node_groups.get("DHALineArt")
        if gn_group:
            new_gn = mesh_obj.modifiers.new(name="DHALineArt", type='NODES')
            new_gn.node_group = gn_group
            for k, v in vals.items():
                if k in new_gn:
                    new_gn[k] = v
            # نقفل القطع من الموديفاير الجديد عشان الميش اتخبزت خلاص
            if preview_type == "_Cut" and "Socket_6" in new_gn:
                new_gn["Socket_6"] = False 
                
    grp_col["es_is_frozen"] = 1
    
    # --- التعديل: إبقاء الواجهة المجمدة محددة ---
    bpy.ops.object.select_all(action='DESELECT')
    frozen_meshes = [o for o in grp_col.objects if o.get("es_is_frozen_mesh")]
    for o in frozen_meshes: o.select_set(True)
    if frozen_meshes: bpy.context.view_layer.objects.active = frozen_meshes[0]
    # ----------------------------------------
    
    clean_orphan_data()
    bpy.context.view_layer.update()

def apply_and_bake_group(grp_name):
    freeze_group(grp_name)

def unfreeze_group(grp_name):
    grp_col = bpy.data.collections.get(grp_name)
    if not grp_col or not grp_col.get("es_is_frozen"): return

    frozen_meshes = [o for o in grp_col.objects if o.get("es_is_frozen_mesh")]
    originals = [o for o in grp_col.objects if o.get("es_is_original_frozen")]

    # --- التعديل الجديد: مزامنة الإعدادات من المجمد للأصلي قبل الحذف ---
    for f_mesh in frozen_meshes:
        p_type = f_mesh.get("es_preview_type")
        # البحث عن الأوبجكت الأصلي المطابق لنفس النوع (Ele, Obj, Cut)
        target_orig = next((o for o in originals if o.get("es_preview_type") == p_type), None)
        
        if target_orig:
            f_gn = f_mesh.modifiers.get("DHALineArt")
            o_gn = target_orig.modifiers.get("DHALineArt")
            
            if f_gn and o_gn:
                # قائمة السوكتات التي نريد الحفاظ على تعديلاتها
                # تشمل: الإزاحة، اللون، السمك، الفتح/الغلق، التهشير، والعمق
                sync_keys = ["Socket_2", "Socket_4", "Socket_5", "Socket_14", "Socket_15", "Socket_16", "Socket_10", "Socket_12"]
                for k in sync_keys:
                    if k in f_gn and k in o_gn:
                        o_gn[k] = f_gn[k]
                
                # تحديث الفيو بورت لضمان ظهور التعديلات فوراً
                o_gn.show_viewport = o_gn.show_viewport
    # ---------------------------------------------------------------

    # حذف الميشات المجمدة بعد نقل بياناتها
    for o in frozen_meshes: bpy.data.objects.remove(o, do_unlink=True)

    originals = [o for o in grp_col.objects if o.get("es_is_original_frozen")]
    
    # --- التعديل: إبقاء الواجهة الحية محددة ---
    bpy.ops.object.select_all(action='DESELECT')
    for o in originals:
        o.hide_viewport = False
        o.hide_render = False
        del o["es_is_original_frozen"]
        o.select_set(True)
        
    if originals: bpy.context.view_layer.objects.active = originals[0]
    # ---------------------------------------

    state_data = grp_col.get("es_frozen_slicer_state")
    if state_data: restore_slicer_state(state_data)

    grp_col["es_is_frozen"] = 0
    clean_orphan_data()
    bpy.context.view_layer.update()

def linear_to_srgb(c):
    if c <= 0.0031308: return c * 12.92
    return 1.055 * math.pow(c, 1/2.4) - 0.055

# ==========================================
# نظام التصدير الشامل (Live & Frozen) مع Lineweights
# ==========================================
def export_preview_group(grp_name, filepath):
    grp_col = bpy.data.collections.get(grp_name)
    if not grp_col: raise Exception("لم يتم العثور على الواجهات!")
    
    # 1. حفظ الحالة الأصلية: هل الجروب كان معمول له فريز أصلاً؟
    was_frozen = grp_col.get("es_is_frozen", 0)
    
    try:
        # 2. تفعيل الفريز مؤقتاً إذا لم يكن مفعلاً (عشان نضمن تحويل كل شيء لميش)
        if not was_frozen:
            print(f">>> Auto-freezing group: {grp_name}")
            freeze_group(grp_name)

        # 3. الحصول على العناصر (دلوقتي كلها ميشات مجمده ومكشوفة في الفيو بورت)
        objs_to_export = [o for o in grp_col.objects if not o.hide_viewport and o.type in {'GREASEPENCIL', 'MESH'} and "es_is_preview" in o]
        if not objs_to_export: raise Exception("الجروب فارغ أو مخفي!")

        # --- بداية الكود الأصلي بتاعك بالكامل (بدون حذف حرف واحد) ---
        cam_name = objs_to_export[0].get("es_cam_name")
        cam_obj = bpy.data.objects.get(cam_name) if cam_name else None

        if cam_obj:
            loc, rot, _ = cam_obj.matrix_world.decompose()
            clean_cam_mat = mathutils.Matrix.LocRotScale(loc, rot, None)
            cam_mat_inv = clean_cam_mat.inverted()
        else:
            cam_mat_inv = mathutils.Matrix.Identity(4)

        doc = ezdxf.new('R2010')
        msp = doc.modelspace()
        total_lines = 0

        for obj in objs_to_export:
            is_gp = obj.type == 'GREASEPENCIL'
            p_type = obj.get("es_preview_type", "")
            
            rgba = (1.0, 1.0, 1.0, 1.0)
            gn_mod = obj.modifiers.get("DHALineArt")
            
            if gn_mod:
                if "Socket_4" in gn_mod: 
                    val = gn_mod["Socket_4"]
                    if not isinstance(val, str):
                        try: rgba = list(val)
                        except TypeError: rgba = val
                elif gn_mod.node_group:
                    for item in gn_mod.node_group.interface.items_tree:
                        if getattr(item, 'identifier', '') == "Socket_4" or getattr(item, 'name', '') == "Color":
                            rgba = list(getattr(item, 'default_value', (1, 1, 1, 1)))
                            break

            try:
                r = int(max(0, min(1, linear_to_srgb(rgba[0]))) * 255)
                g = int(max(0, min(1, linear_to_srgb(rgba[1]))) * 255)
                b = int(max(0, min(1, linear_to_srgb(rgba[2]))) * 255)
                layer_color_int = ezdxf.colors.rgb2int((r, g, b))
            except: layer_color_int = ezdxf.colors.rgb2int((255, 255, 255))

            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj
            
            bpy.ops.object.duplicate(linked=False)
            temp_obj = bpy.context.active_object

            try:
                bpy.ops.object.select_all(action='DESELECT')
                temp_obj.select_set(True)
                bpy.context.view_layer.objects.active = temp_obj
                
                temp_gn = temp_obj.modifiers.get("DHALineArt")
                if temp_gn:
                    temp_gn.show_viewport = True
                    temp_gn.show_render = True
                    # --- 1. تفعيل الزرار السحري للـ Cut ---
                    if p_type in ["_Ele", "_Obj", "_Cut"] and "Socket_17" in temp_gn:
                        temp_gn["Socket_17"] = True

                # --- 2. السر هنا: إجبار بلندر على حساب الخطوط قبل الكونفيرت ---
                bpy.context.view_layer.update()
                # -----------------------------------------------------------

                if is_gp and temp_obj.modifiers.get("Line Art"):
                    try: bpy.ops.object.lineart_bake_static()
                    except: pass

                bpy.ops.object.convert(target='MESH')
                res = bpy.context.active_object
                
                if not res or res.type != 'MESH': 
                    if res: bpy.data.objects.remove(res, do_unlink=True)
                    continue
                    
                res.data.update()
                mesh_mat = res.matrix_world
                bm = bmesh.new()
                bm.from_mesh(res.data)

                if p_type == "_Cut":
                    bm.faces.ensure_lookup_table()
                    
                    hatch_layer_face = bm.faces.layers.int.get('DHAESHATCHINDEX') or bm.faces.layers.float.get('DHAESHATCHINDEX')
                    hatch_layer_vert = bm.verts.layers.int.get('DHAESHATCHINDEX') or bm.verts.layers.float.get('DHAESHATCHINDEX')
                    
                    # --- التعديل: تغيير الاسم وإضافة اللون الفعلي للباوندري ---
                    hatch_outline_layer = f"{grp_name}_ES_HATCH_BOUNDRYLINES"
                    if hatch_outline_layer not in doc.layers:
                        lay = doc.layers.add(name=hatch_outline_layer)
                        lay.dxf.color = 7
                        lay.dxf.true_color = layer_color_int  # أخذ اللون من بلندر
                        lay.dxf.lineweight = 50 
                    
                    for face in bm.faces:
                        hatch_val = 0
                        if hatch_layer_face: hatch_val = int(face[hatch_layer_face])
                        elif hatch_layer_vert: hatch_val = int(face.verts[0][hatch_layer_vert])
                        
                        if hatch_val not in HATCH_DICT: hatch_val = 0
                        h_data = HATCH_DICT[hatch_val]

                        points = []
                        for loop in face.loops:
                            v_world = mesh_mat @ loop.vert.co
                            v_cam = cam_mat_inv @ v_world
                            points.append((v_cam.x, v_cam.y, 0.0)) 
                        
                        if len(points) >= 3:
                            layer_name = f"{grp_name}_{h_data['layer']}"
                            if layer_name not in doc.layers:
                                lay = doc.layers.add(name=layer_name)
                                lay.dxf.color = h_data['color']
                                lay.dxf.lineweight = 13 

                            # إضافة true_color لخطوط الباوندري عشان تتلون
                            msp.add_lwpolyline(points, close=True, dxfattribs={'layer': hatch_outline_layer, 'true_color': layer_color_int})
                            hatch = msp.add_hatch(dxfattribs={'layer': layer_name, 'hatch_style': 0})
                            
                            if h_data['name'] == 'SOLID': hatch.set_solid_fill(color=h_data['color'])
                            else: hatch.set_pattern_fill(h_data['name'], color=h_data['color'], scale=h_data['scale'])
                            
                            hatch.paths.add_polyline_path(points, is_closed=True)
                            total_lines += 1

                    bm.edges.ensure_lookup_table()
                    
                    # --- التعديل: تغيير الاسم وإضافة اللون الفعلي لخطوط القطع ---
                    cut_lines_layer = f"{grp_name}_ES_HATCH_CUTLINES"
                    if cut_lines_layer not in doc.layers:
                        lay = doc.layers.add(name=cut_lines_layer)
                        lay.dxf.color = 7
                        lay.dxf.true_color = layer_color_int # أخذ اللون من بلندر
                        lay.dxf.lineweight = 50 

                    for edge in bm.edges:
                        if len(edge.link_faces) == 0:
                            v1_world = mesh_mat @ edge.verts[0].co
                            v2_world = mesh_mat @ edge.verts[1].co
                            v1_cam = cam_mat_inv @ v1_world
                            v2_cam = cam_mat_inv @ v2_world
                            
                            # إضافة true_color لخطوط القطع عشان تتلون
                            msp.add_line((v1_cam.x, v1_cam.y, 0), (v2_cam.x, v2_cam.y, 0), dxfattribs={'layer': cut_lines_layer, 'true_color': layer_color_int})
                            total_lines += 1

                else:
                    bm.edges.ensure_lookup_table()
                    cue_layer_edge = bm.edges.layers.int.get('DHAESELELINECUEEX') or bm.edges.layers.float.get('DHAESELELINECUEEX')
                    cue_layer_vert = bm.verts.layers.int.get('DHAESELELINECUEEX') or bm.verts.layers.float.get('DHAESELELINECUEEX')
                    
                    # --- التعديل: تغيير الاسم لـ ElevationLines ---
                    base_layer_name = f"{grp_name}_ElevationLines" if p_type == "_Ele" else f"{grp_name}_Furniture"

                    for edge in bm.edges:
                        cue_val = 0
                        if cue_layer_edge: cue_val = int(edge[cue_layer_edge])
                        elif cue_layer_vert: cue_val = int(edge.verts[0][cue_layer_vert])
                        
                        layer_name = f"{base_layer_name}_{cue_val}"
                        if layer_name not in doc.layers:
                            if cue_val == 0: lw = 35
                            elif cue_val == 1: lw = 25
                            elif cue_val == 2: lw = 18
                            elif cue_val == 3: lw = 13
                            elif cue_val == 4: lw = 9
                            else: lw = 5
                            
                            lay = doc.layers.add(name=layer_name)
                            lay.dxf.color = 7
                            lay.dxf.true_color = layer_color_int
                            lay.dxf.lineweight = lw

                        v1_world = mesh_mat @ edge.verts[0].co
                        v2_world = mesh_mat @ edge.verts[1].co
                        v1_cam = cam_mat_inv @ v1_world
                        v2_cam = cam_mat_inv @ v2_world
                        
                        msp.add_line((v1_cam.x, v1_cam.y, 0), (v2_cam.x, v2_cam.y, 0), dxfattribs={'layer': layer_name, 'true_color': layer_color_int})
                        total_lines += 1

                bm.free()
                bpy.data.objects.remove(res, do_unlink=True)
                
            except Exception as e:
                if 'res' in locals() and res and res.name in bpy.data.objects:
                    bpy.data.objects.remove(res, do_unlink=True)
                elif 'temp_obj' in locals() and temp_obj and temp_obj.name in bpy.data.objects:
                    bpy.data.objects.remove(temp_obj, do_unlink=True)
                raise e
        # --- نهاية الكود الأصلي ---

        doc.saveas(filepath)

    finally:
        if not was_frozen:
            print(f">>> Reverting to Live state...")
            unfreeze_group(grp_name)
        else:
            # --- التعديل: إرجاع التحديد للواجهة لو كانت مجمدة أصلاً ---
            grp_col = bpy.data.collections.get(grp_name)
            if grp_col:
                bpy.ops.object.select_all(action='DESELECT')
                frozen_meshes = [o for o in grp_col.objects if o.get("es_is_frozen_mesh")]
                for o in frozen_meshes: o.select_set(True)
                if frozen_meshes: bpy.context.view_layer.objects.active = frozen_meshes[0]