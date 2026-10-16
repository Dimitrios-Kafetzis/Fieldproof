#include "fieldproof_gz_plugins/version.hpp"

#include <string>

#ifndef FIELDPROOF_GZ_PLUGINS_VERSION
#error "FIELDPROOF_GZ_PLUGINS_VERSION must be defined by the build (see CMakeLists.txt)"
#endif

namespace fieldproof_gz_plugins {

std::string Version() {
  return FIELDPROOF_GZ_PLUGINS_VERSION;
}

std::string LibraryName() {
  return "fieldproof_gz_plugins";
}

std::string Banner() {
  return LibraryName() + " " + Version();
}

}  // namespace fieldproof_gz_plugins
