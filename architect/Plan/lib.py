import os
import json
import re
import string

from architect.Plan.models import Site, Member

class PlanValidationError( Exception ):
  pass

CONFIG_FILENAME = '_config'

value_name_regex = re.compile( '^[\+\-\~]?[a-zA-Z0-9_]*$')

member_required = ( 'blueprint', 'complex' ) # 'site' and 'name' are allready take care of

# these should match the default in the model, otherwise the diff may do some funny things
member_default = {
    'blueprint': None,
    'hostname_pattern': '{blueprint}-{id:06}',
    'build_priority': 100,
    'auto_build': False,
    'complex': None,
    'config_values': {},
    'scaler_type': 'none',
    'min_instances': None,
    'max_instances': None,
    'build_ahead': 0,
    'regenerate_rate': 1,
    'tsd_metric': None,
    'lockout_query': None,
    'p_value': None,
    'a_value': None,
    'b_value': None,
    'step_threshold': None,
    'deadband_margin': None,
    'cooldown_seconds': 60,
    'can_grow': False,
    'can_shrink': False,
    'member_affinity': None
}


def _parse_file( pathname, search_path, args ):
  template = string.Template( open( pathname, 'r' ).read() )
  try:
    lines = template.substitute( args ).splitlines()
  except KeyError as e:
    raise PlanValidationError( 'Error applying values to template: "{0}"'.format( e ) )

  raw_data = []
  processed_data = []
  for i in range( 0, len( lines ) ):
    line = lines[i]
    if line.startswith( '#include' ):
      include_parts = line.split()
      try:
        include_file = include_parts[1]
      except KeyError:
        raise PlanValidationError( 'Unalbe to parse innclude line "{0}"'.format( line ) )

      arg_map = args.copy()
      for arg in include_parts[2:]:
        #TODO: regex arg to make sure  it's  valid
        ( key, value ) = arg.split( ':' )
        arg_map[ key ] = value

      include_data = None
      for path in search_path:
        include_pathname = os.path.join( path, include_file )
        if os.path.isfile( include_pathname ):
          include_data = _parse_file( include_pathname, search_path, arg_map )
          break

      if include_data is None:
        raise PlanValidationError( 'Unable to find include file "{0}"'.format( include_file ) )

      if lines[i + 1].strip()[0] == '}':
        processed_data.append( json.dumps( include_data )[1:-1] + '\n' )
      else:
        processed_data.append( json.dumps( include_data )[1:-1] + ',\n' )

      raw_data.append( '' ) # blank line so the line numbers lign up, to bad JSON does do comments
    else:
      raw_data.append( line )
      processed_data.append( line )

  try:
    json.loads( ''.join( raw_data ) ) # the raw thing is so the user can find problems easier
  except ValueError as e:
    raise PlanValidationError( 'Error Parsing Raw "{0}": {1}'.format( pathname, e ) )

  try:
    data = json.loads( ''.join( processed_data ) ) # the raw thing is so the user can find problems easier
  except ValueError as e:
    raise PlanValidationError( 'Error Parsing Pre-Processed "{0}": {1}'.format( pathname, e ) )

  if not isinstance( data, dict ):
    raise PlanValidationError( 'data for file "{0}" is not a dict'.format( pathname ) )

  return data


def _load_member( data, member_name, site_name ):
  member = member_default.copy()
  for name in member:
    try:
      member[ name ] = data[ name ] # make sure type of data[ name] is the same type as member[ name ]
    except KeyError:
      pass

  for name in member_required:
    if member[ name ] is None or member[ name ] == '':
      raise PlanValidationError( 'Expected Field "{0}" is missing from "{1}" in "{2}"'.format( name, member_name, site_name ) )

  return member


def _load_dir( site_dir, search_path, args, site_names=None, member_names=None ):
  if site_names is None:
    site_names = []
  if member_names is None:
    member_names = []

  site = { 'children': {}, 'config_values': {}, 'description': None, 'members': {} }

  try:
    config = json.loads( open( os.path.join( site_dir, CONFIG_FILENAME ), 'r' ).read() )
  except ValueError as e:
    raise PlanValidationError( 'Error Parsing Config for site "{0}": {1}'.format( site_dir, e ) )

  try:
    site_name = config[ 'name' ]
  except KeyError:
    raise PlanValidationError( 'Unable to reterieve site name from site "{0}"'.format( site_dir ) )

  if site_name in site_names:
    raise PlanValidationError( 'Site name "{0}" allready in use'.format( site_name ) )

  site_names.append( site_name )

  try:
    site[ 'description' ] = config[ 'description' ]
  except KeyError:
    raise PlanValidationError( 'Unable to reterieve site description from site "{0}"'.format( site_dir ) )

  value_map = config.get( 'config_values', {} )
  if not isinstance( value_map, dict ):
    raise PlanValidationError( 'Values for site "{0}" is not a dict'.format( site_dir ) )

  for value_name in value_map:
    if not value_name_regex.match( value_name ):
      raise PlanValidationError( 'Value "{0}" in site "{0}" is not valid'.format( value_name, site_dir ) )

  site[ 'config_values' ] = value_map

  for filename in os.listdir( site_dir ):
    if filename == CONFIG_FILENAME:
      continue

    pathname = os.path.join( site_dir, filename )

    if os.path.isdir( pathname ):
      ( name, sub_project ) = _load_dir( pathname, search_path, args, site_names, member_names )
      site[ 'children' ][ name ] = sub_project
      continue

    member_map = _parse_file( pathname, search_path, args )

    for member_name in member_map:
      check_name = '{0}:{1}'.format( member_name, site_name )

      if check_name in member_names:
        raise PlanValidationError( 'Member "{0}" in site "{1}" allready in use'.format( member_name, site_dir ) )

      member_names.append( check_name )

      site[ 'members' ][ member_name ] = _load_member( member_map[ member_name ], member_name, site_name )

  return ( site_name, site )


def _diff_site( path, children ):
  changes = []
  if path == []:
    parent = None
  else:
    parent = path[-1]

  for site_name in children:
    site_path = path + [ site_name ]
    ref_site = children[ site_name ]
    try:
      if parent is None:
        orig_site = Site.objects.get( name=site_name, parent__isnull=True )
      else:
        orig_site = Site.objects.get( name=site_name, parent=parent )

      if ref_site[ 'description' ] != orig_site.description:
        changes.append( ( 'change', site_path, 'site', 'description', orig_site.description, ref_site[ 'description' ] ) )

      if ref_site[ 'config_values' ] != json.loads( orig_site.config_values ):
        changes.append( ( 'change', site_path, 'site', 'config_values', orig_site.config_values, ref_site[ 'config_values' ] ) )

      for member_name in ref_site[ 'members' ]:
        ref_member = ref_site[ 'members' ][ member_name ]
        member_path = site_path + [ member_name ]
        try:
          orig_member = Member.objects.get( site=orig_site, name=member_name )
        except Member.DoesNotExist:
          changes.append( ( 'add', member_path, 'member', ref_member ) )
          continue

        if ref_member[ 'config_values' ] != orig_member.config_values:
          changes.append( ( 'change', member_path, 'member', 'config_values', orig_member.config_values, ref_member[ 'config_values' ] ) )

        for name in ref_member:
          if name == 'config_values':
            continue

          orig_value = getattr( orig_member, name )
          if ref_member[ name ] != orig_value:
             changes.append( ( 'change', member_path, 'member', name, orig_value, ref_member[ name ] ) )

    except Site.DoesNotExist:
      changes.append( ( 'add', site_path, 'site', ref_site[ 'description' ], ref_site[ 'config_values' ] ) )

    changes += _diff_site( site_path, ref_site[ 'children' ] )

  return changes


def load( project_dir, search_path, args ):
  ( site_name, site ) = _load_dir( project_dir, search_path, args )
  return { 'children': { site_name: site } }


def compare_to_running( project ):
  return _diff_site( [], project[ 'children' ] )


def apply_diff( diff_list ):
  for item in diff_list:
    command = item[0]
    path = item[1]
    target = item[2]
    data = item[3:]
    print( 'Applying command "{0}" to "{1}" of type "{2}"'.format( command, '/'.join( path ), target ) )

    if command == 'add':
      if target == 'site':
        site = Site()
        site.name = path[ -1 ]
        site.description = data[0]
        site.config_values = json.dumps( data[1] )
        if len( path ) > 1:
          site.parent = Site.objects.get( name=path[ -2 ] )
        site.clean()
        site.save()

      elif target == 'member':
        member = Member()
        member.name = path[ -1 ]
        member.site = Site.objects.get( name=path[ -2 ] )
        for name in data[0]:
          setattr( member, name, data[0][ name ] )
        member.clean()
        member.save()

      else:
        raise Exception( 'Unknown target type "{0}" for add'.format( target ) )

    elif command == 'change':
      if target == 'site':
        site = Site.objects.get( name=path[ -1 ] )
        if data[0] == 'description':
          site.description = data[2]
          site.clean()
          site.save()

        elif data[0] == 'config_values':
          site.config_values = json.dumps( data[2] )
          site.clean()
          site.save()

      elif target == 'member':
        member = Member.objects.get( site=Site.objects.get( name=path[ -2 ] ), name=path[ -1 ] )
        if data[0] == 'config_values':
          member.config_values = json.dumps( data[2] )
        else:
          setattr( member, data[0], data[2] )
        member.clean()
        member.save()

      else:
        raise Exception( 'Unknown target type "{0}" for change'.format( target ) )
