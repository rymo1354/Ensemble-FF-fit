from abc import ABC, abstractmethod
from parse2fit.core.entries import ReaxEntry
from parse2fit.io.parsers import ParserFactory
from parse2fit.tools.weights import WeightedSampler
import yaml
from pathlib import Path
import os

class YamlManager(ABC):
    def __init__(self, config_path):
        self.config_path = Path(config_path)
        self.config = self.load_config()

    def load_config(self):
        if not self.config_path.exists():
            return {}
        with open(self.config_path, 'r') as file:
            return yaml.safe_load(file) or {}

    def save_config(self):
        with open(self.config_path, 'w') as file:
            yaml.safe_dump(self.config, file)

class ReadWriteFactory(YamlManager):
    def __init__(self, config_path):
        super().__init__(config_path)

    def get_writer(self):
        if not self.config.get('output_format'):
            raise ValueError(f"No 'output_format' found in {self.config_path}")
        elif self.config.get('output_format') == 'jax-reaxff':
            return ReaxRW(self.config)

    def write_output(self, yaml_path):
        pass

class ReaxRW():
    def __init__(self, config_dct):
        self.config_dct = config_dct
        # Reax-specific configuration
        
        self.write_options = ['energy', 'charges', 'forces', 'distances', 
                              'angles', 'dihedrals', 'lattice_vectors']
        self.default_weights = self._set_default_weights(self.config_dct.get('default_weights'))
        self.generation_parameters = self.config_dct.get('generation_parameters')
        self.input_paths = self.config_dct.get('input_paths')

    def _set_default_weights(self, weight_dct):
        dct = {}
        default = {'spread': 0.5}
        default['type'] = 'binary'
        default['min'] = 0
        default['max'] = 1

        for prop in self.write_options:
            if prop in weight_dct:
                if isinstance(weight_dct[prop], dict):
                    dct[prop] = weight_dct[prop]
                else:
                    dct[prop] = default
            else:
                dct[prop] = default
        return dct 
    
    def _default_labeling(self, path, label, num=3):
        if num > 0:
            directory_split = os.path.split(path)
            new_directory = directory_split[0]
            if label == '':
                new_label = directory_split[1]
            else:
               new_label = directory_split[1] + '_' + label
            return self._default_labeling(new_directory, new_label, num-1)
        else:
            return label

    def _get_energy_paths(self):
        relative_energy_paths = []
        for path in self.input_paths:
            if isinstance(self.input_paths[path]['energy'], dict):
                if self.input_paths[path].get('energy') is not None:
                    if self.input_paths[path]['energy'].get('add') is not None:
                        relative_energy_paths += self.input_paths[path]['energy']['add']
                    if self.input_paths[path]['energy'].get('subtract') is not None:
                        relative_energy_paths += self.input_paths[path]['energy']['subtract']
        return relative_energy_paths

    def _get_path_values(self, top_path, root_path):
        # Based on the root path
        path_dct = {}
        supported_properties = ['label', 'structure'] + self.write_options 
        for prop in supported_properties:
            try:
                value = self.input_paths[top_path].get(prop)
            except KeyError: # Can arise for the energy_paths specified, or if value not specified
                value = None
            if prop == 'label':
                if value is None:
                    value = self._default_labeling(root_path, '') # Default 
            elif prop == 'structure': 
                if value is None:
                    value = {'pymatgen_structure': True} # Default
                elif isinstance(value, dict):
                    if value.get('pymatgen_structure') is None and value.get('ase_structure') is None:
                        value['pymatgen_structure'] = True # Default
            elif prop == 'energy': 
                if value is None or value is True:
                    value = {'weighting': self.default_weights['energy']}
                elif isinstance(value, dict): # Dictionary passed
                    if value.get('weighting') is None: # No weighting specified
                        value['weighting'] = self.default_weights['energy']
            else:
                if value is None:
                    pass
                elif value is True:
                    value = {'weighting': self.default_weights[prop]}
                elif isinstance(value, dict):
                     if value.get('weighting') is None: # No weighting specified
                        value['weighting'] = self.default_weights[prop]
            path_dct[prop] = value
        path_dct['top_path'] = top_path
        return path_dct

    def _none_false(self, val):
        if val is None or val is False:
            return False
        else:
            return True

    def _dct_parser(self, objects_dct, path_dct, path):
        parsed_dct = ParserFactory.create_parser(path,
                pymatgen_structure=self._none_false(path_dct['structure'].get('pymatgen_structure')),
                ase_atoms=self._none_false(path_dct['structure'].get('ase_atoms')),
                energy=self._none_false(path_dct.get('energy')),
                charges=self._none_false(path_dct.get('charges')),
                forces=self._none_false(path_dct.get('forces')),
                distances=self._none_false(path_dct.get('distances')),
                angles=self._none_false(path_dct.get('angles')),
                dihedrals=self._none_false(path_dct.get('dihedrals')),
                lattice_vectors=self._none_false(path_dct.get('lattice_vectors'))
                ).parse_all()
        reax_entry = ReaxEntry(label=path_dct['label'])
        reax_entry.from_dict(parsed_dct)
        path_dct['reax_entry'] = reax_entry
        objects_dct[path] = path_dct
        return objects_dct

    def _build_objects_dct(self, objects_dct, paths, walk_roots=True):
        for path in paths:
            if walk_roots == True:
                for root, dirs, files in os.walk(os.path.abspath(path), topdown=True):
                    if root not in list(objects_dct.keys()):
                        path_dct = self._get_path_values(top_path=path, root_path=root) # Dictionary associated with each path
                        try:
                            objects_dct = self._dct_parser(objects_dct, path_dct, root)
                        except ValueError:
                            continue
            else:
                if path not in list(objects_dct.keys()):
                    path_dct = self._get_path_values(top_path=path, root_path=path) # Dictionary associated with each path
                    try:
                        objects_dct = self._dct_parser(objects_dct, path_dct, path)
                    except ValueError: # No DFT code in the root directory being searched
                        continue
        return objects_dct

    def _construct_objects_dct(self):
        sorted_input_paths = sorted(list(self.input_paths.keys()), key=len, reverse=True)
        sorted_energy_paths = sorted(list(self._get_energy_paths()), key=len, reverse=True)
        objects_dct = self._build_objects_dct({}, sorted_input_paths) # Construct top level paths first
        objects_dct = self._build_objects_dct(objects_dct, sorted_energy_paths, walk_roots=False) # Then attempt to construct energy paths
        return objects_dct

    def _get_relative_energies(self, objects_dictionary):
        ''' Continue editing this ''' 
        # Compute weighting across all relative energies
        energy_dct = {} # ReaxObj: {relative_energy: float, add: [], subtract: [], divisors: , weight, sig_figs}
        for object_path in objects_dictionary:
            reax_obj = objects_dictionary[object_path]['reax_entry']
            add_reax_objs, subtract_reax_objs = [], []
            if 'add' in objects_dictionary[object_path]['energy']:
                for add_path in objects_dictionary[object_path]['energy']['add']:
                    add_reax_objs.append(objects_dictionary[add_path]['reax_entry'])
            if 'subtract' in objects_dictionary[object_path]['energy']:
                for subtract_path in objects_dictionary[object_path]['energy']['subtract']:
                    subtract_reax_objs.append(objects_dictionary[subtract_path]['reax_entry'])
            
            if add_reax_objs == [] and subtract_reax_objs == []:
                pass
            else:
                energy_dct[object_path] = {}
                if 'get_divisors' in objects_dictionary[object_path]['energy']:
                    get_divisors = objects_dictionary[object_path]['energy']['get_divisors']
                else:
                    get_divisors = False # Default is to just use 1 for everything, default within the ReaxEntry
                reax_objs, signs, divisors, relative_energy = reax_obj.get_relative_energy(add_reax_objs, 
                                                                                               subtract_reax_objs, 
                                                                                               get_divisors)
                energy_dct[object_path] = {'reax_entry': reax_obj,
                                           'relative_energy': relative_energy.value,
                                           'add': add_reax_objs, 
                                           'subtract': subtract_reax_objs, 
                                           'get_divisors': get_divisors}
        
        ### Get the weights to use from the relative energy values
        super_paths, super_values = [], []
        for energy_path in list(energy_dct.keys()):
            relative_energy = energy_dct[energy_path]['relative_energy']
            weights = objects_dictionary[energy_path]['energy']['weighting']
            #print(energy_path, weights)
            if weights == self.default_weights['energy']: # Top level relative energy weighting
                super_paths.append(energy_path)
                super_values.append(energy_dct[energy_path]['relative_energy'])
            elif isinstance(weights, float):
                energy_dct[energy_path]['weight'] = weights
            elif isinstance(weights, dict): # Different dictionary
                energy_dct[energy_path]['weight'] = WeightedSampler([relative_energy], weights)
        
        super_weights = WeightedSampler(super_values, self.default_weights['energy']).sample()
        for i, super_path in enumerate(super_paths):
            if isinstance(super_weights, float): # Only float returned
                energy_dct[super_path]['weight'] = super_weights
            else:
                energy_dct[super_path]['weight'] = super_weights[i]
        return energy_dct
    
    def get_geo_string(self, objects_dictionary):
        write_string = ''
        for path_i, object_path in enumerate(list(objects_dictionary.keys())):
            reax_obj = objects_dictionary[object_path]['reax_entry']
            structure_args = objects_dictionary[object_path]['structure']
            write_string += reax_obj.structure_to_string(**structure_args)
        return write_string

    def get_trainsetin_string(self, objects_dictionary):
        write_string = ''
        ere = ReaxEntry(label='empty') # For writing headers and footers
        
        # Handling non-Geometry properties
        for write_option in ['charges', 'forces', 'lattice_vectors']:
            write_string += ere.trainsetin_section_header(write_option)
            for path_i, object_path in enumerate(list(objects_dictionary.keys())):
                if objects_dictionary[object_path][write_option] is not None:
                    weights = objects_dictionary[object_path][write_option]['weighting']
                    reax_obj = objects_dictionary[object_path]['reax_entry']
                    write_string += reax_obj.get_property_string(write_option, weights=weights)
            write_string += ere.trainsetin_section_footer(write_option) 
        
        # Handling Geometry Properties
        write_string += ere.trainsetin_section_header('distances')
        for write_option in ['distances', 'angles', 'dihedrals']:
            for path_i, object_path in enumerate(list(objects_dictionary.keys())):
                if objects_dictionary[object_path][write_option] is not None:
                    weights = objects_dictionary[object_path][write_option]['weighting']
                    reax_obj = objects_dictionary[object_path]['reax_entry']
                    write_string += reax_obj.get_property_string('distances', weights=weights)
        write_string += ere.trainsetin_section_footer('distances')

        # Handling energy
        re_dictionary = self._get_relative_energies(objects_dictionary)
        write_string += ere.trainsetin_section_header('energy')
        for path_j, object_path in enumerate(list(re_dictionary.keys())):
            reax_obj = re_dictionary[object_path]['reax_entry']
            weight = re_dictionary[object_path]['weight']
            write_string += reax_obj.relative_energy_to_string(weight=weight, 
                            add=re_dictionary[object_path]['add'], 
                            subtract=re_dictionary[object_path]['subtract'],
                            get_divisors=re_dictionary[object_path]['get_divisors'])
        write_string += ere.trainsetin_section_footer('energy')
        return write_string

    def string_to_file(self, filepath, write_string):
        with open(filepath, "w") as text_file:
            text_file.write(write_string)
        return 

    def write_trainsetins(self, attempt_multiplier=5):
        objects_dictionary = self._construct_objects_dct()
        geo_string = self.get_geo_string(objects_dictionary)

        number_to_generate = self.generation_parameters['number_to_generate']
        path_to_directory = self.generation_parameters['path_to_directory']

        trainsetin_strings = []
        unique = 0
        attempts = 0

        while unique < number_to_generate: # Get unique trainset.in files
            if attempts > attempt_multiplier * number_to_generate:
                break
            trainsetin_string = self.get_trainsetin_string(objects_dictionary)
            if trainsetin_string not in trainsetin_strings:
                write_path = os.path.join(path_to_directory, 'run' + str(unique))
                os.makedirs(write_path, exist_ok=True)
                self.string_to_file(os.path.join(write_path, 'geo'), geo_string)
                self.string_to_file(os.path.join(write_path, 'trainset.in'), trainsetin_string)
                trainsetin_strings.append(trainsetin_string)
                unique += 1
            attempts += 1
        return