from __future__ import print_function
import os
import sys
import numpy as np
import argparse
import json
import re
import tarfile
import textwrap
from zipfile import ZipFile
from xml.etree import ElementTree

if sys.version_info >= (3, 0):
    from os import makedirs
else:
    import errno

    def makedirs(name, mode=511, exist_ok=False):
        try:
            os.makedirs(name, mode=mode)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
            if not exist_ok:
                raise

abs_path = os.path.dirname(os.path.abspath(__file__))
sys.path.append(abs_path)

try:
    from synsetizer import Synsetizer
    _syn_cache = Synsetizer()
except (ImportError, LookupError):
    print("""Warning: NLTK Wordnet is needed for COCO dataset labels.
    pip install nltk
    nltk.download()  # 1. download wordnet from corpus
                     # 2. download brown from corpus
                     # 3. download averaged perceptron model
    """)
    Synsetizer = None
    _syn_cache = None

_valid_extensions = [".jpg", ".png"]
_valid_phases = ["train", "test", "val"]
_synset_label_pattern = re.compile(r'^(?P<LABEL>n\d{8})(?P<EXT>_\d+)?(_(?P<META>\w+))?')
_coco_label_pattern = re.compile(r'^(?P<META>COCO_.*)?(?P<LABEL>\d{12})$')


def listarchive(path, is_root=True, extension_pattern='\.\w+', filter_func=None):
    """Similar to listdir but (in addition to directories) extract archives locally and recurse them
    :param path: the path to start the recursion
    :param is_root: if path is the first in recursion
    :param extension_pattern: regular expression pattern for file extensions
    :param filter_func: a filter to apply on directories and files, to limit the recursion search
    """
    if os.path.isdir(path):
        # Ignore hidden files and directories
        sub_paths = [s0 for s0 in os.listdir(path) if not s0.startswith(".") and not s0.startswith("$")]
        new_filter_func = filter_func
        if filter_func:
            filtered = [s0 for s0 in sub_paths if filter_func(s0)]
            if filtered:
                sub_paths = filtered
                new_filter_func = None  # once matched, recurse all the way
            elif len(sub_paths) > 1:
                # ignore non-matching paths with more than one item
                sub_paths = []
        for s0 in sub_paths:
            s0 = os.path.join(path, s0)
            for sub_path in listarchive(s0, is_root=False,
                                        extension_pattern=extension_pattern,
                                        filter_func=new_filter_func):
                yield sub_path
        return

    local_path, fname = os.path.split(path)
    basename, ext = os.path.splitext(fname)
    ext = ext.lower()
    shortname = basename
    while '.' in shortname:
        shortname, shortext = os.path.splitext(shortname)
        ext = shortext + ext

    if ext not in ['.tar', '.tar.gz', '.zip']:
        if re.match(extension_pattern, ext, flags=re.IGNORECASE):
            yield path
        return

    extracted_path = os.path.join(local_path, 'extracted_' + basename)
    if os.path.exists(extracted_path):
        for sub_path in listarchive(extracted_path, is_root=False,
                                    extension_pattern=extension_pattern,
                                    filter_func=filter_func):
            yield sub_path
        return

    # extract if not yet extracted
    makedirs(extracted_path, exist_ok=True)
    if ext in ['.tar', '.tar.gz']:
        with tarfile.open(path) as archive:
            def is_within_directory(directory, target):
            	
            	abs_directory = os.path.abspath(directory)
            	abs_target = os.path.abspath(target)
            
            	prefix = os.path.commonprefix([abs_directory, abs_target])
            	
            	return prefix == abs_directory
            
            def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
            
            	for member in tar.getmembers():
            		member_path = os.path.join(path, member.name)
            		if not is_within_directory(path, member_path):
            			raise Exception("Attempted Path Traversal in Tar File")
            
            	tar.extractall(path, members, numeric_owner=numeric_owner) 
            	
            
            safe_extract(archive, extracted_path)
    elif ext in ['.zip']:
        with ZipFile(path) as archive:
            archive.extractall(extracted_path)

    # recurse through the just-extracted path
    for sub_path in listarchive(extracted_path, is_root=False,
                                extension_pattern=extension_pattern,
                                filter_func=filter_func):
        yield sub_path

    # remove intermediate *extracted* archives
    if not is_root:
        os.remove(path)


def guess_phase(path):
    """Guess the phase from path
    :param path: file or directory path to guess the phase name from
    :rtype: str
    """
    for elem in reversed(path.replace("\\", "/").split("/")):
        if not elem:
            continue
        elem_lower = elem.lower()
        for name in _valid_phases:
            if name in elem_lower:
                return elem
    return ""


def guess_label(path):
    """Guess the label from path
    :param path: file path to guess the label from
    :rtype: (Union[str,int], str, str)
    """

    for elem in reversed(path.replace("\\", "/").split("/")):
        if not elem:
            continue
        match = _synset_label_pattern.match(elem)
        if match:
            label = match.group('LABEL')
            full_label = label + match.group('EXT') or ''
            return label, full_label, 'IN_' + (match.group('META') or '')

    elem, _ = os.path.splitext(os.path.basename(path))
    match = _coco_label_pattern.match(elem)
    if match:
        full_label = label = match.group('LABEL')
        return int(label), full_label, 'COCO_' + (match.group('META') or '')

    # Use immediate directory as label
    parent = os.path.dirname(path)
    label = _syn_cache.synset_offset(os.path.basename(parent), os.path.basename(elem))
    return label, label, label


def gather_images(root_path, imagedata, counts, max_keep_per_label=np.inf):
    """Create image information structure from images in root_path
    :param root_path: the root directory to start gathering image information
    :param imagedata: a dictionary that will be filled with images location and labels
    :param counts: a dictionary that counts per-label count of images
    :param max_keep_per_label: maximum number of images to keep per-label
    
    Example:
    /root_path/training/n04422727/blue_cheese.jpg
    /root_path/training/n04422727_43.jpg
    /root_path/training/cheeses/n04422727_41.jpg
    /root_path/n04422727_41_training.jpg
    /root_path/training/n04422727_42_bluecheese.jpg
    /root_path/training/COCO_val2014_000000006818.jpg
    """
    for s0 in os.listdir(root_path):
        if s0.startswith(".") or s0.startswith("$"):
            # Ignore hidden files and directories
            continue
        s0 = os.path.join(root_path, s0)
        phase = guess_phase(s0)
        if os.path.isdir(s0):
            gather_images(s0, imagedata, counts, max_keep_per_label=max_keep_per_label)
            continue
        _, file_extension = os.path.splitext(s0)
        if file_extension.lower() not in _valid_extensions:
            continue

        label, full_label, meta = guess_label(s0)

        if label not in counts:
            counts[label] = 1
        elif counts[label] >= max_keep_per_label:
            continue
        else:
            counts[label] += 1

        if not phase:
            for phase in imagedata.keys():
                for name in _valid_phases:
                    if name in phase.lower():
                        break
            if not phase:
                phase = "training"
            print("Phase {} was assumed when processing {}".format(phase, s0))

        if phase not in imagedata:
            imagedata[phase] = [(s0, label, full_label, meta)]
        else:
            imagedata[phase].append((s0, label, full_label, meta))


def get_xml_rects(path, label):
    """Get annotation from VOC-style XML
    """
    rects = []
    tree = ElementTree.parse(path)
    for obj in tree.findall('object'):
        name = obj.find('name').text
        if name != label:
            print('Ignore label "{}" != {} in {}'.format(name, label, path))
            continue
        diff = int(obj.find('difficult').text)
        if diff:
            print('Ignore difficult label "{}" in {}'.format(label, path))
            continue
        for bndbox in obj.findall('bndbox'):
            # bndbox does not seem to be 1-based, because there are some that start at 0
            rect = [int(bndbox.find(k).text) for k in ['xmin', 'ymin', 'xmax', 'ymax']]
            rects.append(rect)

    return rects


def get_coco_bboxes(path):
    """Parse bboxes from json in the path
    """
    with open(path) as f:
        content = json.load(f)

    annotations = content['annotations']
    categories = {cat['id']: (cat['name'], cat['supercategory']) for cat in content['categories']}
    del content

    bboxes = {}
    for ann in annotations:
        image_id = int(ann['image_id'])
        cat_id = int(ann['category_id'])
        name, parent = categories[cat_id]
        label = _syn_cache.synset_offset(name, parent)
        bbox = ann['bbox']
        bbox[2] += bbox[0] - 1
        bbox[3] += bbox[1] - 1
        if image_id not in bboxes:
            bboxes[image_id] = [{'class': label, 'rect': bbox}]
        else:
            bboxes[image_id].append({'class': label, 'rect': bbox})

    return bboxes


def get_boxes(phase, full_label, label, meta, annotations, phase_cache):
    """Get the list of boxes for a label
    :rtype list
    """

    if meta.startswith('COCO_'):
        if not _syn_cache:
            raise Exception("""Wordnet not found.
            Install nltk and wordnet:
            pip install nltk
            nltk.download()  # 1. download wordnet from corpus
                             # 2. download brown from corpus
                             # 3. download averaged perceptron model
            """)

        for ann_path in annotations:
            for path in listarchive(ann_path, extension_pattern='\.json'):
                fname = os.path.basename(path)
                if 'instances' in fname and phase in fname:
                    if fname not in phase_cache:
                        phase_cache[fname] = get_coco_bboxes(path)
                    if label not in phase_cache[fname]:
                        # COCO needs category id from the annotation file
                        return []
                    return phase_cache[fname][label]

    def voc_ann_filter(elem):
        if label in elem:
            return True

    for ann_path in annotations:
        for path in listarchive(ann_path, extension_pattern='\.xml', filter_func=voc_ann_filter):
            fname = os.path.basename(path)
            if fname.startswith(full_label):
                return [{'class': label, 'rect': rect} for rect in get_xml_rects(path, label)]

    boxes = [{'class': label, 'rect': [0, 0, 0, 0]}]  # full image as a box
    return boxes


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='Process images in a path recursively and prepare TSV files (ImageNet and COCO).',
        epilog=textwrap.dedent('''Example:
convert_to_tsv.py d:/data/imagenet/ -a d:/data/imagenet/Annotation.tar.gz
convert_to_tsv.py d:/data/coco -a d:/data/coco/annotations_trainval2017.zip
convert_to_tsv.py d:/data/fridge -a d:/data/coco/annotations_fridge/
'''))

    parser.add_argument('-k', '--keep', help='Maximum number of images to keep for each label',
                        type=float, default=np.inf)
    parser.add_argument('-a', '--annotation', action='append', required=True, default=[],
                        help='Annotation archive file, or directory (can be specified multiple times)')
    parser.add_argument('root_path', metavar='PATH', help='path to the images dataset')

    if len(sys.argv) == 1:
        parser.print_help()
        raise Exception("Required input not provided")

    args = parser.parse_args()
    root_path = args.root_path
    max_keep_per_label = args.keep

    images = {}
    counts = {}
    gather_images(root_path, images, counts, max_keep_per_label=max_keep_per_label)

    # noinspection PyTypeChecker
    multi_phase = len(images.keys()) > 1
    for phase, vs in images.items():
        if multi_phase:
            print("Phase: {}".format(phase))
        phase_cache = {}
        with open(os.path.join(root_path, phase + '.lineidx'), "w") as idx_file:
            with open(os.path.join(root_path, phase + '.tsv'), "w") as tsv_file:
                for v in vs:
                    path, label, full_label, meta = v
                    relpath = os.path.relpath(path, root_path).replace("\\", "/")
                    boxes = get_boxes(phase, full_label, label, meta, args.annotation, phase_cache)
                    if not boxes:
                        print("No annotation for {}".format(path))
                        continue
                    idx_file.write("{}\n".format(tsv_file.tell()))
                    tsv_file.write("{}\t{}\t{}\n".format(full_label, json.dumps(boxes), relpath))

    return images, counts

if __name__ == '__main__':
    main_results = main()
